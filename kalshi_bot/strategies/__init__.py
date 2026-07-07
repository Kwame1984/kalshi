"""Strategy registry — single source of truth for backtest runner and
execution engine."""

from kalshi_bot.strategies.base import Strategy
from kalshi_bot.strategies.cumulative_momentum import CumulativeMomentum
from kalshi_bot.strategies.expiry_convergence import ExpiryConvergence
from kalshi_bot.strategies.inplay_favorite import InPlayFavorite
from kalshi_bot.strategies.long_horizon_carry import LongHorizonCarry
from kalshi_bot.strategies.options_rv import OptionsRV
from kalshi_bot.strategies.perception_gap import PerceptionGap
from kalshi_bot.strategies.politics_favorite import PoliticsFavorite
from kalshi_bot.strategies.sports_devig import SportsDevig
from kalshi_bot.strategies.tail_seller import TailSeller
from kalshi_bot.strategies.theta_harvest import ThetaHarvest
from kalshi_bot.strategies.weather_edge import WeatherEdge

STRATEGIES: dict[str, type[Strategy]] = {
    "cumulative_momentum": CumulativeMomentum,
    "expiry_convergence": ExpiryConvergence,
    "inplay_favorite": InPlayFavorite,
    "long_horizon_carry": LongHorizonCarry,
    "options_rv": OptionsRV,
    "perception_gap": PerceptionGap,
    "politics_favorite": PoliticsFavorite,
    "sports_devig": SportsDevig,
    "tail_seller": TailSeller,
    "theta_harvest": ThetaHarvest,
    "weather_edge": WeatherEdge,
}
