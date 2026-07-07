"""Strategy registry — single source of truth for backtest runner and
execution engine."""

from kalshi_bot.strategies.base import Strategy
from kalshi_bot.strategies.expiry_convergence import ExpiryConvergence
from kalshi_bot.strategies.theta_harvest import ThetaHarvest
from kalshi_bot.strategies.weather_edge import WeatherEdge

STRATEGIES: dict[str, type[Strategy]] = {
    "expiry_convergence": ExpiryConvergence,
    "theta_harvest": ThetaHarvest,
    "weather_edge": WeatherEdge,
}
