# Express MVC Example

This repository contains a simple Express.js application that follows a basic Model‑View‑Controller (MVC) pattern. The project is designed to be easy to run locally and ready for deployment to services like Heroku, Vercel, Render or any other Node.js hosting platform.

## Features

* **Express 4.x** – Minimal and flexible Node.js web application framework
* **MVC Structure** – Separates concerns between controllers, routes and (optionally) models
* **Test Endpoint** – An example `/test` API endpoint that returns a JSON message
* **Configurable Port** – Uses an environment variable or defaults to `3000` when running locally
* **Health Check** – Provides a `/health` endpoint to monitor uptime when deployed

## Project Structure

```
express-mvc-app/
│
├── app.js            # Main entry point for the application
├── package.json      # Project metadata and dependencies
├── controllers/      # Route logic separated into controller modules
│   └── testController.js
├── routes/           # Route definitions mapping paths to controllers
│   └── testRoutes.js
├── config/           # Configuration files (e.g. database connections)
│   └── datasource.js # Currently empty; ready for future configuration
├── models/           # Place to add data models (empty by default)
├── .gitignore        # Files and directories to ignore in version control
└── README.md         # This documentation
```

## Getting Started

Ensure you have Node.js and npm installed on your machine. Then follow these steps:

```bash
cd express-mvc-app
npm install       # Install dependencies listed in package.json
npm start         # Start the Express server
```

The server will start on port `3000` by default. You can override the port by setting the `PORT` environment variable before running the server:

```bash
PORT=4000 npm start
```

### Hitting the `/test` Endpoint

After starting the server, you can verify that the API is working by visiting:

```
http://localhost:3000/test
```

This will return a JSON response similar to:

```json
{
  "message": "Test endpoint is working"
}
```

### Deploying

This project is ready for deployment on any service that supports Node.js. Typical deployment steps include:

1. **Install dependencies**: `npm install`
2. **Set environment variables**: such as `PORT` and any database configuration in `config/datasource.js`
3. **Run the server**: `npm start`

Most platforms detect the `start` script in `package.json` and run your application automatically. A `/health` endpoint is available for health checks during deployment.

Feel free to extend this project by adding models, additional controllers, routes, or integration with a database inside `config/datasource.js`.