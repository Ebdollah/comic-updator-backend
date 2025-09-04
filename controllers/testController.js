// Controller class for the test routes.
// This class contains methods that handle the logic for corresponding routes and
// return responses to the client. Using a class-based approach promotes
// better organization, testability, and follows modern JavaScript patterns.

class TestController {
  /**
   * Test endpoint handler
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async test(req, res) {
    try {
      // Respond with a simple JSON object to confirm that the API endpoint is working.
      res.status(200).json({ 
        message: 'Test endpoint is working',
        timestamp: new Date().toISOString(),
        method: req.method,
        url: req.url
      });
    } catch (error) {
      console.error('Error in test controller:', error);
      res.status(500).json({ 
        error: 'Internal server error',
        message: 'Something went wrong while processing your request'
      });
    }
  }

  /**
   * Health check endpoint handler
   * @param {Object} req - Express request object
   * @param {Object} res - Express response object
   */
  async health(req, res) {
    try {
      res.status(200).json({ 
        status: 'healthy',
        timestamp: new Date().toISOString(),
        uptime: process.uptime()
      });
    } catch (error) {
      console.error('Error in health check:', error);
      res.status(500).json({ 
        status: 'unhealthy',
        error: 'Health check failed'
      });
    }
  }
}

// Export an instance of the controller
export default new TestController();