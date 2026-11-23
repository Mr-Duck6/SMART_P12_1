# Project tree

```text
coap-http-openhab-gateway/
├── docker-compose.yml
├── README.md
├── .gitignore
├── gateway/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── __init__.py
│       └── main.py
├── openhab/
│   └── conf/
│       ├── items/coap.items
│       ├── rules/gateway.rules
│       ├── sitemaps/coap.sitemap
│       ├── things/coap-gateway.things
│       ├── services/addons.cfg
│       └── transform/jsonpath/README.md
└── tests/smoke_test.py
```
