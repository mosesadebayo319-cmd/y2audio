const fs = require('node:fs');
const required = [
  'vendor/bin/node',
  'vendor/bin/ffmpeg',
  'vendor/bin/ffprobe',
];
const missing = required.filter((file) => !fs.existsSync(file));
if (missing.length) {
  console.error(`Missing media runtime: ${missing.join(', ')}`);
  process.exit(1);
}
console.log('Media runtimes ready');
