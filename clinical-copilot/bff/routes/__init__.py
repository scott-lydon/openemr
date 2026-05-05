"""BFF route modules.

The historical pattern for the BFF defined every route on the FastAPI
app object inside ``bff/main.py``. The Week 2 ``documents`` endpoint
adds a second concern (multipart streaming, scanner orchestration) that
is large enough to warrant its own module.

Use the routers exported here in ``bff/main.py``::

    from bff.routes import documents
    app.include_router(documents.router)
"""
