"""
STAC -> EO3 utilities
"""

import datetime
import uuid
from collections import namedtuple
from copy import deepcopy
from functools import partial, singledispatch
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
)
from warnings import warn

import pystac.asset
import pystac.item
from affine import Affine
from datacube.index.eo3 import prep_eo3
from datacube.index.index import default_metadata_type_docs
from datacube.model import Dataset, DatasetType, metadata_from_doc
from datacube.utils.geometry import CRS, GeoBox, Geometry
from pystac.extensions.eo import EOExtension
from pystac.extensions.item_assets import ItemAssetsExtension
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.raster import RasterExtension
from odc.index import odc_uuid
from toolz import dicttoolz

T = TypeVar("T")
BandMetadata = namedtuple("BandMetadata", ["dtype", "nodata", "units"])
ConversionConfig = Dict[str, Any]

EPSG4326 = CRS("EPSG:4326")

# Mapping between EO3 field names and STAC properties object field names
# EO3 metadata was defined before STAC 1.0, so we used some extensions
# that are now part of the standard instead
STAC_TO_EO3_RENAMES = {
    "end_datetime": "dtr:end_datetime",
    "start_datetime": "dtr:start_datetime",
    "gsd": "eo:gsd",
    "instruments": "eo:instrument",
    "platform": "eo:platform",
    "constellation": "eo:constellation",
    "view:off_nadir": "eo:off_nadir",
    "view:azimuth": "eo:azimuth",
    "view:sun_azimuth": "eo:sun_azimuth",
    "view:sun_elevation": "eo:sun_elevation",
}


(_eo3,) = [
    metadata_from_doc(d) for d in default_metadata_type_docs() if d.get("name") == "eo3"
]


def with_default(v: Optional[T], default_value: T) -> T:
    """
    Replace None with default value

    :param v: Value that might be None
    :param default_value: Default value of the same type as v
    :return: ``v`` unles it is ``None`` then return ``default_value`` instead
    """
    if v is None:
        return default_value
    return v


def band_metadata(asset: pystac.asset.Asset, default: BandMetadata) -> BandMetadata:
    """
    Compute band metadata from Asset raster extension with defaults from default.

    :param asset: Asset with raster extension
    :param default: Values to use for fallback
    :return: BandMetadata tuple constructed from raster:bands metadata
    """
    try:
        rext = RasterExtension.ext(asset)
    except pystac.ExtensionNotImplemented:
        return default

    if rext.bands is None or len(rext.bands) == 0:
        return default

    if len(rext.bands) > 1:
        warn(f"Defaulting to first band of {len(rext.bands)}")
    band = rext.bands[0]

    return BandMetadata(
        with_default(band.data_type, default.dtype),
        with_default(band.nodata, default.nodata),
        with_default(band.unit, default.units),
    )


def has_proj_ext(item: pystac.Item) -> bool:
    """
    Check if STAC Item has projection extension
    """
    try:
        ProjectionExtension.validate_has_extension(item, add_if_missing=False)
        return True
    except pystac.ExtensionNotImplemented:
        return False


def has_proj_data(asset: pystac.asset.Asset) -> bool:
    """
    :returns: True if both ``.shape`` and ``.transform`` are set
    :returns: False if either ``.shape`` or ``.transform`` are missing
    """
    prj = ProjectionExtension.ext(asset)
    return prj.shape is not None and prj.transform is not None


def is_raster_data(asset: pystac.asset.Asset, check_proj: bool = False) -> bool:
    """
    - Has "data" role --> True
    - Has roles other than "data" --> False
    - Has no role but
      - media_type has ``image/``

    :param asset:
       STAC Asset to check

    :param check_proj:
       when enabled check if asset is part of an Item that has projection
       extension enabled and if yes only consider bands with
       projection data as "raster data" bands.
    """
    if check_proj:
        if (
            asset.owner is not None
            and has_proj_ext(asset.owner)
            and not has_proj_data(asset)
        ):
            return False

    if asset.roles is not None and len(asset.roles) > 0:
        return "data" in asset.roles
    return "image/" in asset.media_type


def _mk_1x1_geobox(geom: Geometry) -> GeoBox:
    """
    Construct 1x1 pixels GeoBox tightly enclosing supplied geometry.

    :param geom: Geometry in whatever projection
    :return: GeoBox object such that geobox.extent.contains(geom) is True, geobox.shape == (1,1)
    """
    x1, y1, x2, y2 = (*geom.boundingbox,)
    # note that Y axis is inverted
    #   0,0 -> X_min, Y_max
    #   1,1 -> X_max, Y_min
    return GeoBox(1, 1, Affine((x2 - x1), 0, x1, 0, (y1 - y2), y2), geom.crs)


def asset_geobox(asset: pystac.asset.Asset) -> GeoBox:
    """
    Compute GeoBox from STAC Asset.

    This only works if ProjectionExtension is used with the
    following properties populated:

    - shape
    - transform
    - CRS

    :raises ValueError: when transform,shape or crs are missing
    :raises ValueError: when transform is not Affine.
    """
    try:
        _proj = ProjectionExtension.ext(asset)
    except pystac.ExtensionNotImplemented:
        raise ValueError("No projection extension defined") from None

    if _proj.shape is None or _proj.transform is None or _proj.crs_string is None:
        raise ValueError(
            "The asset must have the following fields (from the projection extension):"
            " shape, transform, and one of an epsg, wkt2, or projjson"
        )

    h, w = _proj.shape
    if len(_proj.transform) not in (6, 9):
        raise ValueError("Asset transform must be 6 or 9 elements in size")

    if len(_proj.transform) == 9 and _proj.transform[-3:] != [0, 0, 1]:
        raise ValueError(f"Asset transform is not affine: {_proj.transform}")

    affine = Affine(*_proj.transform[:6])
    return GeoBox(w, h, affine, _proj.crs_string)


def geobox_gsd(geobox: GeoBox) -> float:
    """
    Compute ground sampling distance of a given GeoBox.
    """
    return min(map(abs, [geobox.transform.a, geobox.transform.e]))


def compute_eo3_grids(
    assets: Dict[str, pystac.asset.Asset]
) -> Tuple[Dict[str, GeoBox], Dict[str, str]]:
    """
    Compute a minimal set of eo3 grids, pick default one, give names to
    non-default grids, while keeping track of which asset has which grid

    Assets must have ProjectionExtension with shape, transform and crs information
    populated.
    """

    def gbox_name(geobox: GeoBox) -> str:
        gsd = geobox_gsd(geobox)
        return f"g{gsd:g}"

    geoboxes = dicttoolz.valmap(asset_geobox, assets)

    # GeoBox to list of bands that share same footprint
    grids: Dict[GeoBox, List[str]] = {}
    crs: Optional[CRS] = None

    for k, geobox in geoboxes.items():
        grids.setdefault(geobox, []).append(k)

    # Default grid is the one with highest count of bands
    #   If there is a tie pick one with the smallest ground sampling distance
    def gbox_score(geobox: GeoBox) -> Tuple[int, float]:
        return (-len(grids[geobox]), geobox_gsd(geobox))

    # locate default grid
    g_default, *_ = sorted(grids, key=gbox_score)

    named_grids: Dict[str, GeoBox] = {}
    band2grid: Dict[str, str] = {}
    for grid, bands in grids.items():
        if crs is None:
            crs = grid.crs
        elif grid.crs != crs:
            raise ValueError("Expect all assets to share common CRS")

        grid_name = "default" if grid is g_default else gbox_name(grid)
        if grid_name in named_grids:
            raise NotImplementedError(
                "TODO: deal with multiple grids with same sampling distance"
            )

        named_grids[grid_name] = grid
        for band in bands:
            band2grid[band] = grid_name

    return named_grids, band2grid


def _band2grid_from_gsd(assets: Dict[str, pystac.asset.Asset]) -> Dict[str, str]:
    grids: Dict[float, List[str]] = {}
    for name, asset in assets.items():
        gsd = asset.common_metadata.gsd
        gsd = 0 if gsd is None else gsd
        gsd_normed = float(f"{gsd:g}")
        grids.setdefault(gsd_normed, []).append(name)

    # Default grid is one with largest number of bands
    # .. and lowest gsd when there is a tie
    (_, default_gsd), *_ = sorted((-len(bands), gsd) for gsd, bands in grids.items())
    band2grid = {}
    for gsd, bands in grids.items():
        grid_name = "default" if gsd == default_gsd else f"g{gsd:g}"
        for band in bands:
            band2grid[band] = grid_name

    return band2grid


def alias_map_from_eo(item: pystac.Item, quiet: bool = False) -> Dict[str, str]:
    """
    Generate mapping ``common name -> canonical name`` for all unique common names defined on the Item eo extension.

    :param item: STAC Item to process
    :type item: pystac.Item
    :param quiet: Do not print warning if duplicate common names are found, defaults to False
    :type quiet: bool, optional
    :return: common name to canonical name mapping
    :rtype: Dict[str, str]
    """
    try:
        bands = EOExtension.ext(item, add_if_missing=False).bands
    except pystac.ExtensionNotImplemented:
        return {}

    common_names: Dict[str, Set[str]] = {}
    for band in bands:
        common_name = band.common_name
        if common_name is not None:
            common_names.setdefault(common_name, set()).add(band.name)

    def _aliases(common_names):
        for alias, bands in common_names.items():
            if len(bands) == 1:
                (band,) = bands
                yield (alias, band)
            elif not quiet:
                warn(f"Common name `{alias}` is repeated, skipping")

    return dict(_aliases(common_names))


def normalise_product_name(name: str) -> str:
    """
    Create valid product name from arbitrary string
    """

    # TODO: for now just map `-`,` ` to `_`
    return name.replace("-", "_").replace(" ", "_")


def _band_metadata(v: Union[BandMetadata, Dict[str, Any]]) -> BandMetadata:
    if isinstance(v, BandMetadata):
        return v
    return BandMetadata(
        v.get("dtype", "uint16"), v.get("nodata", 0), v.get("units", "1")
    )


def mk_product(
    name: str,
    bands: Iterable[str],
    cfg: Dict[str, Any],
    aliases: Optional[Dict[str, str]] = None,
) -> DatasetType:
    """
    Generate ODC Product from simplified config.

    :param name: Product name
    :type name: str
    :param bands: List of band names
    :type bands: Iterable[str]
    :param cfg: Band configuration, band_name -> Config mapping
    :type cfg: Dict[str, Any]
    :param aliases: Map of aliases ``alias -> band name``
    :type aliases: Optional[Dict[str, str]], optional
    :return: Constructed ODC Product with EO3 metadata type
    :rtype: DatasetType
    """

    if aliases is None:
        aliases = {}

    _cfg: Dict[str, BandMetadata] = {
        name: _band_metadata(meta) for name, meta in cfg.items()
    }
    band_aliases: Dict[str, List[str]] = {}
    for alias, canonical_name in aliases.items():
        band_aliases.setdefault(canonical_name, []).append(alias)

    def make_band(
        name: str, cfg: Dict[str, BandMetadata], band_aliases: Dict[str, List[str]]
    ) -> Dict[str, Any]:
        info = cfg.get(name, cfg.get("*", BandMetadata("uint16", 0, "1")))
        aliases = band_aliases.get(name)

        doc = {
            "name": name,
            "dtype": info.dtype,
            "nodata": info.nodata,
            "units": info.units,
        }
        if aliases is not None:
            doc["aliases"] = aliases
        return doc

    doc = {
        "name": normalise_product_name(name),
        "metadata_type": "eo3",
        "measurements": [make_band(band, _cfg, band_aliases) for band in bands],
    }
    return DatasetType(_eo3, doc)


@singledispatch
def infer_dc_product(x: Any, cfg: Optional[ConversionConfig] = None) -> DatasetType:
    raise TypeError("Invalid type, must be one of: pystac.Item, pystac.Collection")


@infer_dc_product.register(pystac.Item)
def infer_dc_product_from_item(
    item: pystac.Item, cfg: Optional[ConversionConfig] = None
) -> DatasetType:
    """
    :param item: Sample STAC Item from a collection
    :param cfg: Dictionary of configuration, see below

    .. code-block:: yaml

       sentinel-2-l2a:  # < name of the collection, i.e. ``.collection_id``
         measurements:
           "*":  # Band named "*" contains band info for "most" bands
             dtype: uint16
             nodata: 0
             units: "1"
           SCL:  # Those bands that are different than "most"
             dtype: uint8
             nodata: 0
             units: "1"
         aliases:  #< unique alias -> canonical map
           rededge: B05
           rededge1: B05
           rededge2: B06
           rededge3: B07
         uuid:   # Rules for constructing UUID for Datasets (PLANNED, not implemented yet)
           mode: auto   # auto|random|native(expect .id to contain valid UUID string)
           extras:      # List of extra keys from properties to include (mode=auto)
           - "s2:generation_time"

         warnings: ignore  # ignore|all  (default all)

       some-other-collection:
         measurements:
         #...
       "*": # Applies to all collections if not defined on a collection
         warnings: ignore
    """
    if cfg is None:
        cfg = {}

    collection_id = item.collection_id
    _cfg = cfg.get("*", {})
    _cfg.update(cfg.get(collection_id, {}))

    warnings: str = _cfg.get("warnings", "all")
    quiet = warnings == "ignore"

    data_bands: Dict[str, pystac.asset.Asset] = dicttoolz.valfilter(
        partial(is_raster_data, check_proj=True), item.assets
    )

    aliases = alias_map_from_eo(item, quiet=quiet)
    aliases.update(_cfg.get("aliases", {}))

    # 1. If band in user config -- use that
    # 2. Use data from raster extension (with fallback to "*" config)
    # 3. Use config for "*" from user config as fallback
    band_cfg = _cfg.get("measurements", {})
    band_defaults = _band_metadata(band_cfg.get("*", {}))
    for name, asset in data_bands.items():
        if name not in band_cfg:
            bm = band_metadata(asset, band_defaults)
            if bm is not band_defaults:
                band_cfg[name] = bm

    product = mk_product(collection_id, data_bands, band_cfg, aliases)

    # We assume that grouping of data bands into grids is consistent across
    # entire collection, so we compute it once and keep it on a product object
    # at least for now.
    if has_proj_ext(item):
        _, band2grid = compute_eo3_grids(data_bands)
    else:
        band2grid = _band2grid_from_gsd(data_bands)

    _cfg["band2grid"] = band2grid
    product._stac_cfg = _cfg  # pylint: disable=protected-access
    return product


def _compute_uuid(
    item: pystac.Item, mode: str = "auto", extras: Optional[Sequence[str]] = None
) -> uuid.UUID:
    if mode == "native":
        return uuid.UUID(item.id)
    if mode == "random":
        return uuid.uuid4()

    assert mode == "auto"
    # 1. see if .id is already a UUID
    try:
        return uuid.UUID(item.id)
    except ValueError:
        pass

    # 2. .id, .collection_id, [extras]
    _extras = (
        {} if extras is None else {key: item.properties.get(key, "") for key in extras}
    )
    return odc_uuid(item.collection_id, "stac", [], stac_id=item.id, **_extras)


def item_to_ds(item: pystac.Item, product: DatasetType) -> Dataset:
    """
    Construct Dataset object from STAC Item and previosuly constructed Product.

    :raises ValueError: when not all assets share the same CRS
    """
    # pylint: disable=too-many-locals
    _cfg = getattr(product, "_stac_cfg", {})
    band2grid: Dict[str, str] = _cfg.get("band2grid", {})

    has_proj = has_proj_ext(item)
    measurements: Dict[str, Dict[str, Any]] = {}
    grids: Dict[str, Dict[str, Any]] = {}
    crs = None
    _assets = item.assets

    for band in product.measurements:
        asset = _assets.get(band, None)
        if asset is None:
            warn(f"Missing asset with name: {band}")
            continue
        measurements[band] = {"path": asset.href}

        # Only compute grids when proj extension is enabled
        if not has_proj:
            continue

        grid_name = band2grid.get(band, "default")
        if grid_name != "default":
            measurements[band]["grid"] = grid_name

        if grid_name not in grids:
            geobox = asset_geobox(_assets[band])
            grids[grid_name] = dict(shape=geobox.shape, transform=geobox.transform)
            if crs is None:
                crs = geobox.crs
            elif crs != geobox.crs:
                raise ValueError(
                    "Expect all assets to share common CRS"
                )  # pragma: no cover

    # No proj metadata make up 1x1 Grid in EPSG4326 instead
    if not has_proj:
        geom = Geometry(item.geometry, EPSG4326)
        geobox = _mk_1x1_geobox(geom)
        crs = geobox.crs
        grids["default"] = dict(shape=geobox.shape, transform=geobox.transform)

    assert crs is not None

    uuid_cfg = _cfg.get("uuid", {})
    ds_uuid = _compute_uuid(
        item, mode=uuid_cfg.get("mode", "auto"), extras=uuid_cfg.get("extras", [])
    )

    ds_doc = {
        "id": str(ds_uuid),
        "$schema": "https://schemas.opendatacube.org/dataset",
        "crs": str(crs),
        "grids": grids,
        "location": "",
        "measurements": measurements,
        "properties": dicttoolz.keymap(
            lambda k: STAC_TO_EO3_RENAMES.get(k, k), item.properties
        ),
        "lineage": {},
    }

    return Dataset(product, prep_eo3(ds_doc), uris=[ds_doc.get("location", "")])


def stac2ds(
    items: Iterable[pystac.Item], cfg: Optional[ConversionConfig] = None
) -> Iterator[Dataset]:
    """
    Given a lazy sequence of STAC Items turn it into a lazy sequence of ``Dataset`` objects
    """
    products: Dict[str, DatasetType] = {}
    for item in items:
        product = products.get(item.collection_id)

        # Have not seen this collection yet, figure it out
        if product is None:
            product = infer_dc_product(item, cfg)
            products[item.collection_id] = product

        yield item_to_ds(item, product)


def _mk_sample_item(collection: pystac.Collection) -> pystac.Item:
    try:
        item_assets = ItemAssetsExtension.ext(collection).item_assets
    except pystac.ExtensionNotImplemented:
        raise ValueError(
            "This only works on Collections with ItemAssets extension"
        ) from None

    item = pystac.Item(
        "sample",
        None,
        None,
        datetime.datetime(2020, 1, 1),
        {},
        stac_extensions=collection.stac_extensions,
        collection=collection,
    )

    for name, asset in item_assets.items():
        _asset = dict(href="")
        _asset.update(asset.to_dict())
        item.add_asset(name, pystac.asset.Asset.from_dict(_asset))

    return item


@infer_dc_product.register(pystac.Collection)
def infer_dc_product_from_collection(
    collection: pystac.Collection, cfg: Optional[ConversionConfig] = None
) -> DatasetType:
    if cfg is None:
        cfg = {}
    return infer_dc_product(_mk_sample_item(collection), cfg)