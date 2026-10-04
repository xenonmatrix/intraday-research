# Persistent trader

The current documentation is [README.md](README.md), [configuration reference](docs/CONFIGURATION.md), [architecture](docs/ARCHITECTURE.md) and [operator runbook](docs/OPERATIONS.md).

Historical date-bound experiments are retained locally for audit and unit-test compatibility. They are not the production authorization path. The canonical production configuration is `config/trader.json`; the checked-in example is `config/trader.example.json`. Migration never grants LIVE permission or resets prior session losses, signals, orders or positions.
