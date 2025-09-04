// This file is reserved for defining your data source configuration.
// You might export database connection details, ORM configuration,
// or any other sources of persistent data for your application.

/**
 * Database configuration object
 * This can be extended to include connection details for various databases
 */
const databaseConfig = {
  // Example MongoDB configuration
  mongodb: {
    uri: process.env.MONGODB_URI || 'mongodb://localhost:27017/express-mvc-app',
    options: {
      useNewUrlParser: true,
      useUnifiedTopology: true,
    }
  },
  
  // Example PostgreSQL configuration
  postgresql: {
    host: process.env.DB_HOST || 'localhost',
    port: process.env.DB_PORT || 5432,
    database: process.env.DB_NAME || 'express_mvc_app',
    username: process.env.DB_USER || 'postgres',
    password: process.env.DB_PASSWORD || 'password',
  },
  
  // Example Redis configuration
  redis: {
    host: process.env.REDIS_HOST || 'localhost',
    port: process.env.REDIS_PORT || 6379,
    password: process.env.REDIS_PASSWORD || undefined,
  }
};

/**
 * Get database configuration based on environment
 * @param {string} type - Database type (mongodb, postgresql, redis)
 * @returns {Object} Database configuration object
 */
export const getDatabaseConfig = (type = 'mongodb') => {
  return databaseConfig[type] || databaseConfig.mongodb;
};

/**
 * Environment configuration
 */
export const config = {
  nodeEnv: process.env.NODE_ENV || 'development',
  port: process.env.PORT || 3000,
  apiVersion: 'v1',
  cors: {
    origin: process.env.CORS_ORIGIN || '*',
    credentials: true
  }
};

export default {
  database: databaseConfig,
  config
};

