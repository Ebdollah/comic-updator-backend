# Comic Updator Backend

A FastAPI-based backend service for the Comic Updator application with Firebase integration.

## Features

- FastAPI framework with async support
- Firebase Firestore database integration
- Production-ready Docker configuration
- Environment-based configuration management
- Structured logging
- Health check endpoints
- CORS support
- Security best practices

## Project Structure

```
comic-updator-backend/
├── app/
│   ├── __init__.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── api.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── api.py
│   │       └── health.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   └── logging.py
│   ├── models/
│   │   └── __init__.py
│   ├── services/
│   │   ├── __init__.py
│   │   └── firebase.py
│   └── utils/
│       └── __init__.py
├── main.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── docker-compose.dev.yml
├── env.example
├── env.production
└── README.md
```

## Quick Start

### Development Setup

1. **Clone and setup environment:**
   ```bash
   git clone <repository-url>
   cd comic-updator-backend
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp env.example .env
   # Edit .env with your Firebase credentials
   ```

3. **Run the application:**
   ```bash
   python main.py
   ```

### Docker Development

```bash
# Using Docker Compose for development
docker-compose -f docker-compose.dev.yml up --build
```

### Production Deployment

1. **Configure production environment:**
   ```bash
   cp env.production .env
   # Edit .env with your production Firebase credentials
   ```

2. **Build and run with Docker:**
   ```bash
   docker-compose up --build -d
   ```

## API Endpoints

- `GET /` - Root endpoint with API information
- `GET /api/v1/health` - Basic health check
- `GET /api/v1/health/detailed` - Detailed health check with service status
- `GET /docs` - Interactive API documentation (Swagger UI)

## Environment Variables

Copy `env.example` to `.env` and configure:

- **Firebase Settings**: Add your Firebase project credentials
- **Security**: Change the secret key for production
- **CORS**: Configure allowed origins for your frontend
- **Server**: Adjust host and port as needed

## Firebase Setup

1. Create a Firebase project
2. Enable Firestore Database
3. Generate a service account key
4. Add the credentials to your `.env` file

## Development

### Code Quality

```bash
# Format code
black .
isort .

# Lint code
flake8 .

# Run tests
pytest
```

### Adding New Features

1. Create new routes in `app/api/v1/`
2. Add models in `app/models/`
3. Add services in `app/services/`
4. Update the API router in `app/api/v1/api.py`

## Production Considerations

- Change `SECRET_KEY` in production
- Configure proper CORS origins
- Use environment variables for all sensitive data
- Monitor logs and health endpoints
- Use HTTPS in production
- Configure proper Firebase security rules

## Health Monitoring

The application provides health check endpoints for monitoring:

- Basic health: `/api/v1/health`
- Detailed health: `/api/v1/health/detailed`

These endpoints check:
- Application status
- Firebase connectivity
- Service availability

## License

[Add your license here]
