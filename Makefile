# Development scripts

# Install dependencies
install:
	pip install -r requirements.txt

# Run development server
dev:
	uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Run production server
prod:
	gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000

# Format code
format:
	black .
	isort .

# Lint code
lint:
	flake8 .

# Run tests
test:
	pytest

# Docker development
docker-dev:
	docker-compose -f docker-compose.dev.yml up --build

# Docker production
docker-prod:
	docker-compose up --build -d

# Clean up
clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
