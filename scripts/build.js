#!/usr/bin/env node

/**
 * Build script for Express MVC App
 * This script performs various checks and validations before deployment
 */

import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.join(__dirname, '..');

console.log('🚀 Starting build process...\n');

// Check Node.js version
const nodeVersion = process.version;
const requiredVersion = '14.0.0';
const currentVersion = nodeVersion.slice(1); // Remove 'v' prefix

console.log(`📋 Node.js version: ${nodeVersion}`);

if (currentVersion < requiredVersion) {
  console.error(`❌ Node.js version ${currentVersion} is below required version ${requiredVersion}`);
  process.exit(1);
}

// Check if package.json exists
const packageJsonPath = path.join(projectRoot, 'package.json');
if (!fs.existsSync(packageJsonPath)) {
  console.error('❌ package.json not found');
  process.exit(1);
}

// Check if main entry point exists
const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'));
const mainFile = path.join(projectRoot, packageJson.main);

if (!fs.existsSync(mainFile)) {
  console.error(`❌ Main entry point ${packageJson.main} not found`);
  process.exit(1);
}

console.log('✅ Main entry point found');

// Syntax check for main files
const filesToCheck = [
  'app.js',
  'controllers/testController.js',
  'routes/testRoutes.js',
  'config/datasource.js'
];

console.log('\n📋 Running syntax checks...');
for (const file of filesToCheck) {
  const filePath = path.join(projectRoot, file);
  if (fs.existsSync(filePath)) {
    try {
      execSync(`node -c "${filePath}"`, { stdio: 'pipe' });
      console.log(`✅ ${file} - syntax OK`);
    } catch (error) {
      console.error(`❌ ${file} - syntax error`);
      console.error(error.message);
      process.exit(1);
    }
  }
}

// Check for required dependencies
console.log('\n📋 Checking dependencies...');
try {
  const packageLockPath = path.join(projectRoot, 'package-lock.json');
  if (!fs.existsSync(packageLockPath)) {
    console.log('⚠️  package-lock.json not found - run npm install');
  } else {
    console.log('✅ package-lock.json found');
  }
} catch (error) {
  console.error('❌ Error checking dependencies:', error.message);
}

// Environment validation
console.log('\n📋 Environment validation...');
const nodeEnv = process.env.NODE_ENV || 'development';
console.log(`Environment: ${nodeEnv}`);
console.log(`Port: ${process.env.PORT || '3000'}`);

// Create build info
const buildInfo = {
  timestamp: new Date().toISOString(),
  nodeVersion: nodeVersion,
  environment: nodeEnv,
  buildVersion: packageJson.version
};

const buildInfoPath = path.join(projectRoot, 'build-info.json');
fs.writeFileSync(buildInfoPath, JSON.stringify(buildInfo, null, 2));
console.log('✅ Build info created');

console.log('\n🎉 Build completed successfully!');
console.log('📦 Ready for deployment');
