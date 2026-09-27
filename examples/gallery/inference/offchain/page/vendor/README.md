# Vendored browser hashing

@noble/hashes 1.8.0, MIT. Source: https://github.com/paulmillr/noble-hashes/tree/1.8.0
Copied from the installed dashboard dependency. Changes: utils.js imports
./crypto.js instead of the package export; one trailing space is removed. No network/CDN loading.
The Cowboy wallet provider holds keys and signs secp256k1 digests.

Original SHA-256 hashes:

```json
{
  "sha3.js": "0260b46f92a3a94c7179958600d7f92469ef2c8f5d2e966bd9a838ff815713e9",
  "_u64.js": "e48c0cfc10810439a4807b46db136ce603a3fa09b62584f513ef2f3ca496af54",
  "utils.js": "4cf4c1e05affedcb4fd584a43d76ae1a3711e34a36e2251b90c27e33ecc74fad",
  "crypto.js": "9211d026c5d21e60e0126dd6f01150d87da5ba7261b8f468215c1264372ff5a5"
}
```
