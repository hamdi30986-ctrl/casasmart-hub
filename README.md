# casasmart-hub

CasaSmart's Home Assistant custom integration — the hub-side core that runs on
every CasaSmart hub. Installs into `custom_components/casasmart/`.

## Layout

```
custom_components/casasmart/   # the integration
  storage/                     # SQLite (WAL) + JSON config
tests/                         # stdlib unittest, no dependencies
docs/                          # module docs
```

## Requirements

- Home Assistant (Container or OS)
- Python 3.11+

## Run tests

```bash
python3 -m unittest discover -s tests -v
```

## License

Proprietary — © CasaSmart. All rights reserved.
