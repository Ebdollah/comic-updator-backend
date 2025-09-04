// Routes related to the test endpoints. This file defines the URL paths
// and maps them to controller methods.

import express from 'express';
import testController from '../controllers/testController.js';

const router = express.Router();

// Route: GET /test
// Description: Responds with a test message to confirm that the API is working.
router.get('/test', testController.test);

// Route: GET /health
// Description: Health check endpoint for monitoring and deployment platforms.
router.get('/health', testController.health);

export default router;