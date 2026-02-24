# Installation

```bash
pip install netbox-data-import
```

Add to `PLUGINS` in `configuration.py`:

```python
PLUGINS = ["netbox_data_import"]
```

Run migrations:

```bash
python manage.py migrate
```
