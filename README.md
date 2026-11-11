# qlib-data-ops

Safely update and manage a local [Qlib](https://github.com/microsoft/qlib) China A-share dataset built from the
[chenditc/investment_data](https://github.com/chenditc/investment_data) community release.

This package ships tooling only. It does not redistribute market data. It is not an official Microsoft project.

## Commands

```bash
qdata status               # every local Qlib dataset at a glance
qdata cn status            # A-share data: local date, pools, backups, remote release
qdata cn update            # replace with the latest community package
qdata cn health-check      # structure checks plus a real Qlib read
qdata cn restore --list    # rollback backups
```

## Tests

```bash
.venv/bin/python -m pytest
```

## License

MIT
