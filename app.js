// Main entry point for the Express application.
// This file sets up the Express server, registers middlewares and routes,
// and starts listening on the configured port.

import express from 'express';
import path from 'path';
import { fileURLToPath } from 'url';
import testRoutes from './routes/testRoutes.js';

// Get __dirname equivalent for ES modules
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Create an Express application
const app = express();

// Middleware to parse incoming JSON requests
app.use(express.json());
// Middleware to parse URL‑encoded data
app.use(express.urlencoded({ extended: true }));

// CORS middleware for development
app.use((req, res, next) => {
  res.header('Access-Control-Allow-Origin', '*');
  res.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept, Authorization');
  
  if (req.method === 'OPTIONS') {
    res.sendStatus(200);
  } else {
    next();
  }
});

// Register routes. Additional route modules can be added here.
app.use('/', testRoutes);

// Root route
app.get('/', (req, res) => {
  res.status(200).json({ 
    message: 'Welcome to Express MVC App',
    version: '1.0.0',
    endpoints: {
      test: '/test',
      health: '/health'
    }
  });
});

// 404 handler for undefined routes
app.use('*', (req, res) => {
  res.status(404).json({ 
    error: 'Route not found',
    message: `The requested route ${req.originalUrl} does not exist`,
    availableRoutes: ['/', '/test', '/health']
  });
});

// Global error handler
app.use((err, req, res, next) => {
  console.error('Global error handler:', err);
  res.status(500).json({ 
    error: 'Internal server error',
    message: 'Something went wrong on the server'
  });
});

// Configure the port. Default to 3000 for local development.
const PORT = process.env.PORT || 3000;

// Start the server
app.listen(PORT, () => {
  console.log(`🚀 Server is running on port ${PORT}`);
  console.log(`📱 Health check: http://localhost:${PORT}/health`);
  console.log(`🧪 Test endpoint: http://localhost:${PORT}/test`);
});

export default app;