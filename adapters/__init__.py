from __future__ import annotations

from .base import BasketItem, MarketplaceAdapter, PriceResult
from .c5game import C5GameAdapter
from .csgoskins import build_csgoskins_adapters, clear_csgoskins_cache
from .csfloat import CSFloatAdapter
from .dmarket import DMarketAdapter
from .haloskins import HaloSkinsAdapter
from .marketcsgo import MarketCSGOAdapter
from .openskin import build_openskin_adapters, clear_openskin_cache
from .skindeck import SkindeckAdapter
from .skinport import SkinportAdapter, clear_skinport_cache
from .uuskins import UUSkinsAdapter
from .webpage import PriceCompareWebAdapter
from .waxpeer import WaxpeerAdapter


def build_adapter_registry() -> dict[str, MarketplaceAdapter]:
    clear_csgoskins_cache()
    clear_openskin_cache()
    clear_skinport_cache()
    adapters: list[MarketplaceAdapter] = [
        HaloSkinsAdapter(),
        CSFloatAdapter(),
        WaxpeerAdapter(),
        C5GameAdapter(),
        DMarketAdapter(),
        MarketCSGOAdapter(),
        SkinportAdapter(),
        SkindeckAdapter(),
        UUSkinsAdapter(),
        *build_openskin_adapters(),
        *build_csgoskins_adapters(),
        PriceCompareWebAdapter(),
    ]
    return {adapter.key: adapter for adapter in adapters}


__all__ = [
    "BasketItem",
    "MarketplaceAdapter",
    "PriceResult",
    "build_adapter_registry",
]
