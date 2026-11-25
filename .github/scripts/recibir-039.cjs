const { createDecipheriv, privateDecrypt, constants } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const tmp = process.env.RUNNER_TEMP;
try {
  const { wrapped, iv, tag, ciphertext } = JSON.parse(fs.readFileSync(path.join(tmp, 'ww-entrega.json'), 'utf8'));
  const key = privateDecrypt({
    key: fs.readFileSync(path.join(tmp, 'ww-recv.pem')),
    oaepHash: 'sha256',
    padding: constants.RSA_PKCS1_OAEP_PADDING,
  }, Buffer.from(wrapped, 'base64'));
  const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(iv, 'base64'));
  decipher.setAuthTag(Buffer.from(tag, 'base64'));
  const decrypted = Buffer.concat([decipher.update(Buffer.from(ciphertext, 'base64')), decipher.final()]);
  fs.writeFileSync(path.join(tmp, 'ww-entrada.json'), decrypted, { mode: 0o600 });
} catch {
  console.error('La entrega cifrada no corresponde a este intento de publicación.');
  process.exitCode = 1;
}
