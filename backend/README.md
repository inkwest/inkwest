# Inkwest Backend

AI-powered Kubernetes debugging assistant backend built with FastAPI.

## Development

```bash
# Install dependencies
uv venv && uv pip install -e ".[dev]"

# Run migrations
python db_migrate.py

# Start the server
uvicorn app.main:app --reload
```

## Testing

```bash
uv run pytest tests/ -v
```

## API Documentation

Once running, visit:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
