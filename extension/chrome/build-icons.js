const sharp = require('sharp');
const fs = require('fs');
const path = require('path');

const sizes = [16, 32, 48, 128];
const inputSvg = './jdtojson-gemini-original.svg';
const outDir = './images';

if (!fs.existsSync(outDir)) {
  fs.mkdirSync(outDir);
}

sizes.forEach(size => {
  const outputFile = path.join(outDir, `icon-${size}.png`);

  sharp(inputSvg)
    .resize(size, size)
    .png()
    .toFile(outputFile)
    .then(info => console.log(`Generated: icon-${size}.png (${info.width}x${info.height})`))
    .catch(err => console.error(`Error generating icon-${size}.png:`, err));
});
