const path = require('path');

module.exports = {
  webpack: {
    configure: (webpackConfig) => {
      // Remove source-map-loader rules to suppress missing source map warnings.
      if (webpackConfig.module && webpackConfig.module.rules) {
        webpackConfig.module.rules = webpackConfig.module.rules.filter((rule) => {
          if (rule.loader && rule.loader.includes('source-map-loader')) {
            return false;
          }
          if (Array.isArray(rule.use)) {
            rule.use = rule.use.filter((u) => typeof u === 'string' ? !u.includes('source-map-loader') : !(u.loader && u.loader.includes('source-map-loader')));
          }
          return true;
        });
      }
      
      // Also, ensure our fallback settings for Node.js modules remain.
      webpackConfig.resolve.fallback = {
        fs: false,
        os: require.resolve('os-browserify/browser'),
        path: require.resolve('path-browserify'),
        process: require.resolve('process/browser'),
        util: require.resolve('util/'),
        assert: require.resolve('assert/'),
        stream: require.resolve('stream-browserify'),
        buffer: require.resolve('buffer/'),
      };

      return webpackConfig;
    },
  },
};
