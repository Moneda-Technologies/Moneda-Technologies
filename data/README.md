# Moneda static data

These JSON files are bootstrap/catalogue inputs. MongoDB remains authoritative
at runtime when the application is connected to MongoDB.

```text
data/
├── catalog/
│   ├── product_types.json
│   ├── blankets/       # blanket products, options, categories and bars
│   ├── underpacking/   # MPack products, types and options
│   └── chemicals/      # chemical products, options and categories
├── machines/           # the dedicated blanket machine catalogue
├── pricing/            # EUR master and customer-type pricing sources
├── geography/          # country/region catalogue
└── tax/                # tax rules
```

The backend resolves this grouped layout through
`backend/app/services/data_paths.py`. A legacy flat-layout fallback is kept so
an existing deployment can be upgraded safely, but new files should be added
to the appropriate grouped directory and registered in the path map.
