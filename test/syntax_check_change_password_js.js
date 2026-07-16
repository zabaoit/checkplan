const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const target = path.join(root, 'web', 'static', 'change_password.js');
const source = fs.readFileSync(target, 'utf8');

new Function(source);
console.log(`OK ${path.relative(root, target)}`);
