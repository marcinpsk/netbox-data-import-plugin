# CHANGELOG

<!-- version list -->

## v1.6.1 (2026-08-26)

### Bug Fixes

- Close the three review follow-ups deferred from #103
  ([#115](https://github.com/marcinpsk/netbox-data-import-plugin/pull/115),
  [`0893a85`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0893a850c675499219562ce048d864d76c3d774b))


## v1.6.0 (2026-08-25)

### Continuous Integration

- Audit the workflows with zizmor and guard the release job
  ([#100](https://github.com/marcinpsk/netbox-data-import-plugin/pull/100),
  [`7e8e680`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/7e8e680b6a75428b545fe417ea2cd6fc72dbd95c))

### Features

- Add the target-field catalog, adapter registry, and profile cut…
  ([#103](https://github.com/marcinpsk/netbox-data-import-plugin/pull/103),
  [`9cce873`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9cce873398f8114fb726ef5a6de76574abf051a4))


## v1.5.2 (2026-08-18)

### Bug Fixes

- Drop --offline from the release build command
  ([#99](https://github.com/marcinpsk/netbox-data-import-plugin/pull/99),
  [`8d8ea20`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/8d8ea205efd0a5850036be372dd5bd96a0cdaf14))

- Placement, contact picker, preview interaction, and device import storage
  ([#87](https://github.com/marcinpsk/netbox-data-import-plugin/pull/87),
  [`ad1bcbe`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ad1bcbec890d1fc2559e3b9e8a277b2d207ea14a))

### Chores

- Mount the postgres 18 data parent directory
  ([#76](https://github.com/marcinpsk/netbox-data-import-plugin/pull/76),
  [`a138987`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a13898764a38a8330e27148836e9088ae2f50550))


## v1.5.1 (2026-08-17)

### Bug Fixes

- Improve contact resolution and preview actions
  ([#75](https://github.com/marcinpsk/netbox-data-import-plugin/pull/75),
  [`1a612cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/1a612cce76f5fb8840f381686854540762ce0dc5))


## v1.5.0 (2026-08-14)

### Chores

- **deps**: Bump the github-actions group with 2 updates
  ([#73](https://github.com/marcinpsk/netbox-data-import-plugin/pull/73),
  [`84c67d7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/84c67d77bf5083ffe72e7b18e17b996803231fb9))

- **deps-dev**: Bump pytest-django from 4.12.0 to 4.13.0
  ([#71](https://github.com/marcinpsk/netbox-data-import-plugin/pull/71),
  [`28ce416`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/28ce416b6b497bc71c291a0176c9ff44e8df05aa))

- **deps-dev**: Bump ruff from 0.16.1 to 0.16.2
  ([#72](https://github.com/marcinpsk/netbox-data-import-plugin/pull/72),
  [`09fbf70`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/09fbf707069291fcc544e20931e9543f56787f77))

### Features

- Sync native contacts and improve import progress
  ([#74](https://github.com/marcinpsk/netbox-data-import-plugin/pull/74),
  [`da2e026`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/da2e0269cf0663d0bc953ac5bcad075330b385c8))


## v1.4.2 (2026-08-11)

### Bug Fixes

- Check that PR titles follow Conventional Commits
  ([#70](https://github.com/marcinpsk/netbox-data-import-plugin/pull/70),
  [`6ee531f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6ee531f19b3b5eeb64fa8c3a63c15ce7925032cf))

### Chores

- Add CODEOWNERS to auto-request @marcinpsk on PRs
  ([`d613def`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d613def827b2ab4cbe248854f23ed1763eb9a837))

- Cover .github/CODEOWNERS in REUSE.toml
  ([#57](https://github.com/marcinpsk/netbox-data-import-plugin/pull/57),
  [`15ea082`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/15ea0822dc92dadc4f4714c8867ffb9e017af044))

- **deps**: Bump actions/checkout from 6.0.3 to 7.0.0 in the github-actions group
  ([#49](https://github.com/marcinpsk/netbox-data-import-plugin/pull/49),
  [`e3ad795`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e3ad795c74f2855cd859403242fa6154338fd910))

- **deps**: Bump actions/setup-python from 6.2.0 to 6.3.0 in the github-actions group
  ([#52](https://github.com/marcinpsk/netbox-data-import-plugin/pull/52),
  [`fdc723a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fdc723a88ea8a9847a52f425f38b160d4f2a031a))

- **deps**: Bump github/codeql-action from 4.35.5 to 4.36.0 in the github-actions group
  ([#41](https://github.com/marcinpsk/netbox-data-import-plugin/pull/41),
  [`4e24fa2`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4e24fa2339c2c61d2c8488e95fe5e2759e0fbb44))

- **deps**: Bump github/codeql-action from 4.36.1 to 4.36.2 in the github-actions group
  ([#44](https://github.com/marcinpsk/netbox-data-import-plugin/pull/44),
  [`38496de`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/38496de6ce9e270b2b906cac7b51f5c4181ed72c))

- **deps**: Bump gitpython from 3.1.50 to 3.1.54 in the uv group across 1 directory
  ([#61](https://github.com/marcinpsk/netbox-data-import-plugin/pull/61),
  [`daec75c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/daec75c91825c79c18f21a8a416cda1a16ab5061))

- **deps**: Bump pymdown-extensions from 10.21.3 to 11.0 in the uv group across 1 directory
  ([#65](https://github.com/marcinpsk/netbox-data-import-plugin/pull/65),
  [`27f3092`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/27f3092645f6a74a4229e2465be8afdb0ddf8bf6))

- **deps**: Bump pymdown-extensions from 11.0 to 11.0.1 in the uv group across 1 directory
  ([#68](https://github.com/marcinpsk/netbox-data-import-plugin/pull/68),
  [`4349205`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4349205be5e849e7ee42fc00ec3bf9c8dc6ada2f))

- **deps**: Bump the github-actions group with 2 updates
  ([#64](https://github.com/marcinpsk/netbox-data-import-plugin/pull/64),
  [`2d4337b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2d4337b2c15953992800a036544eecf88a685b42))

- **deps**: Bump the github-actions group with 3 updates
  ([#67](https://github.com/marcinpsk/netbox-data-import-plugin/pull/67),
  [`34e88ba`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/34e88ba3059def08fcd80c44bf85e095e7597906))

- **deps**: Bump the github-actions group with 3 updates
  ([#54](https://github.com/marcinpsk/netbox-data-import-plugin/pull/54),
  [`d978287`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d978287e007bb1ecd402e7efd18a4395f6a9a3dd))

- **deps**: Bump the github-actions group with 3 updates
  ([#42](https://github.com/marcinpsk/netbox-data-import-plugin/pull/42),
  [`9bbf1cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9bbf1cc137e9c238b7240d774cec65a5a0849d41))

- **deps**: Bump the github-actions group with 6 updates
  ([#60](https://github.com/marcinpsk/netbox-data-import-plugin/pull/60),
  [`2aab48e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2aab48edee137fdecb47fe3abf237b09f540b04d))

- **deps-dev**: Bump build from 1.5.0 to 1.5.1
  ([#56](https://github.com/marcinpsk/netbox-data-import-plugin/pull/56),
  [`bd0e27d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/bd0e27dba8261dff41489451c5f9f7e337e3d0bc))

- **deps-dev**: Bump mkdocs-material from 9.7.6 to 9.7.7
  ([#58](https://github.com/marcinpsk/netbox-data-import-plugin/pull/58),
  [`876ba7e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/876ba7e17f3e5c315aa26653101e84e119db5db1))

- **deps-dev**: Bump pre-commit from 4.6.0 to 4.6.1
  ([#62](https://github.com/marcinpsk/netbox-data-import-plugin/pull/62),
  [`f8c0577`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/f8c0577ac754462fe970801867395e8b74651165))

- **deps-dev**: Bump pytest from 9.0.3 to 9.1.0
  ([#46](https://github.com/marcinpsk/netbox-data-import-plugin/pull/46),
  [`b55d2f8`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b55d2f8115d6ff987133eb02e9f6c664db08e064))

- **deps-dev**: Bump pytest from 9.1.0 to 9.1.1
  ([#48](https://github.com/marcinpsk/netbox-data-import-plugin/pull/48),
  [`6c834c5`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/6c834c53cb8917ad1568fade765aeeec6564dea2))

- **deps-dev**: Bump python-semantic-release from 10.5.3 to 10.6.1
  ([#53](https://github.com/marcinpsk/netbox-data-import-plugin/pull/53),
  [`fda8f48`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fda8f484dbf8f6683e156f793765fea2fd0cb767))

- **deps-dev**: Bump ruff from 0.15.13 to 0.15.14
  ([#40](https://github.com/marcinpsk/netbox-data-import-plugin/pull/40),
  [`36b0e63`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/36b0e63e40e4768d6828d7458190c5980d483f44))

- **deps-dev**: Bump ruff from 0.15.14 to 0.15.15
  ([#43](https://github.com/marcinpsk/netbox-data-import-plugin/pull/43),
  [`c6d2e85`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c6d2e85dfafd4f8b08761677e227119ce1ef2c6c))

- **deps-dev**: Bump ruff from 0.15.15 to 0.15.16
  ([#45](https://github.com/marcinpsk/netbox-data-import-plugin/pull/45),
  [`2f5a5ba`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2f5a5ba39ac36293ec7456f1defc80d9a645aa71))

- **deps-dev**: Bump ruff from 0.15.16 to 0.15.17
  ([#47](https://github.com/marcinpsk/netbox-data-import-plugin/pull/47),
  [`d15a0d3`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d15a0d3a84acde811ae0d97d30d012ac36f15b48))

- **deps-dev**: Bump ruff from 0.15.17 to 0.15.18
  ([#50](https://github.com/marcinpsk/netbox-data-import-plugin/pull/50),
  [`91c1f7b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/91c1f7bdf24308f67f3df02f1c5666e1fd64b294))

- **deps-dev**: Bump ruff from 0.15.18 to 0.15.20
  ([#51](https://github.com/marcinpsk/netbox-data-import-plugin/pull/51),
  [`9b0668d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9b0668d89b79ccdc5f51c525f8d94c2ce246b89f))

- **deps-dev**: Bump ruff from 0.15.20 to 0.15.21
  ([#55](https://github.com/marcinpsk/netbox-data-import-plugin/pull/55),
  [`9bc572f`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9bc572f6b36adaa2bc319deb3a82b8f393825ff3))

- **deps-dev**: Bump ruff from 0.15.21 to 0.15.22
  ([#59](https://github.com/marcinpsk/netbox-data-import-plugin/pull/59),
  [`9b7352d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9b7352da7b883c6a96e479fed1a95a55d5a63b95))

- **deps-dev**: Bump ruff from 0.15.22 to 0.16.0
  ([#63](https://github.com/marcinpsk/netbox-data-import-plugin/pull/63),
  [`c32a6c7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c32a6c728c0971131a8ca6dcac0a53101867ae66))

- **deps-dev**: Bump ruff from 0.16.0 to 0.16.1
  ([#66](https://github.com/marcinpsk/netbox-data-import-plugin/pull/66),
  [`3265559`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/3265559e5682ef572783c0255cae9e3ef615ad44))


## v1.4.1 (2026-05-21)

### Bug Fixes

- Allow extra_json:* target_field values in profile import
  ([#39](https://github.com/marcinpsk/netbox-data-import-plugin/pull/39),
  [`fe8f7cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/fe8f7ccbfc6d3f08331309277faba39299694d2d))

### Chores

- **deps**: Bump github/codeql-action from 4.35.4 to 4.35.5 in the github-actions group
  ([#38](https://github.com/marcinpsk/netbox-data-import-plugin/pull/38),
  [`550fe8a`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/550fe8a4bf4939548e82181e7417d0ef076a4c45))


## v1.4.0 (2026-05-19)

### Features

- **sync**: Rack sync, face dependency guard, and atomic placement sync
  ([#36](https://github.com/marcinpsk/netbox-data-import-plugin/pull/36),
  [`04bc9d7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/04bc9d718a605467bd12c9c7b6de0288fcebacc8))


## v1.3.1 (2026-05-16)

### Bug Fixes

- **preview**: Show split button for all unlinked device rows regardless of name separator
  ([#35](https://github.com/marcinpsk/netbox-data-import-plugin/pull/35),
  [`e4ea3ab`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e4ea3ab8c50808ea39b98f44b9d37f0361811843))


## v1.3.0 (2026-05-16)

### Features

- **split-modal**: Field preview and conflict detection
  ([#34](https://github.com/marcinpsk/netbox-data-import-plugin/pull/34),
  [`5175f55`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5175f55bd031efad0795e83fafda202f1818df4d))


## v1.2.3 (2026-05-15)

### Bug Fixes

- **engine**: Skip name-based auto-match for duplicate device names
  ([#31](https://github.com/marcinpsk/netbox-data-import-plugin/pull/31),
  [`d25c4cc`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d25c4ccb191bcdd5ec984b988674dfe5aa10d136))


## v1.2.2 (2026-05-15)

### Bug Fixes

- Surface missing device role on preview and form validation
  ([#30](https://github.com/marcinpsk/netbox-data-import-plugin/pull/30),
  [`e60893b`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e60893b7817d1e07ae37db1df1aaf7977cabf09d))


## v1.2.1 (2026-05-15)

### Bug Fixes

- Error badge count ([#27](https://github.com/marcinpsk/netbox-data-import-plugin/pull/27),
  [`c639b14`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c639b14d92116a1ed36230f907b3ce20f83138dd))

### Chores

- **deps**: Bump github/codeql-action from 4.35.3 to 4.35.4 in the github-actions group
  ([#26](https://github.com/marcinpsk/netbox-data-import-plugin/pull/26),
  [`0eabe8e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/0eabe8e001c0b5cf00af3c877785eee4cb74ac07))


## v1.2.0 (2026-05-12)

### Chores

- **deps**: Bump actions/upload-artifact from 7.0.0 to 7.0.1 in the github-actions group
  ([#13](https://github.com/marcinpsk/netbox-data-import-plugin/pull/13),
  [`5e5a5b0`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5e5a5b0b9a133c41ca61f2153d7fd321bf8e76de))

- **deps**: Bump github/codeql-action from 4.35.2 to 4.35.3 in the github-actions group
  ([#25](https://github.com/marcinpsk/netbox-data-import-plugin/pull/25),
  [`b512a37`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b512a3719206f75c99117a9f0f49cd15a481600f))

- **deps**: Bump the github-actions group with 2 updates
  ([#15](https://github.com/marcinpsk/netbox-data-import-plugin/pull/15),
  [`37e11eb`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/37e11eb400daa3bd6a29263839d171853d7e8a56))

### Features

- Per-row sync — ⚡ Sync to NetBox button on import preview
  ([#20](https://github.com/marcinpsk/netbox-data-import-plugin/pull/20),
  [`e2761eb`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/e2761eba1f643e1301d3340d76451c043c53e8dc))

- Rack type mapping, dark theme fixes, unignore bug fix, modal UX
  ([#14](https://github.com/marcinpsk/netbox-data-import-plugin/pull/14),
  [`77bcb1e`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/77bcb1e86eaa7ee6cac5abde5d3c232414ee85e1))


## v1.0.3 (2026-04-14)

### Bug Fixes

- Netbox import export ([#12](https://github.com/marcinpsk/netbox-data-import-plugin/pull/12),
  [`8b167aa`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/8b167aa2bd29a91b53169ccda1f4bd913a332a9d))


## v1.0.2 (2026-04-09)

### Bug Fixes

- Packaging issue ([#11](https://github.com/marcinpsk/netbox-data-import-plugin/pull/11),
  [`2a3632c`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/2a3632c3fc96934c76fb07855baab8c41de68296))

### Chores

- Update dependabot
  ([`ac9f4ac`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/ac9f4ac1761502692be98446d5644de9cc22c654))

- Update pyproject
  ([`d5655de`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/d5655de50df1109b686da31d503c9e6d157bc13f))

- **deps**: Bump github/codeql-action from 4.33.0 to 4.34.1 in the github-actions group
  ([#8](https://github.com/marcinpsk/netbox-data-import-plugin/pull/8),
  [`caa5ced`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/caa5ced70d976075e617d7566328d4fc31c59866))

- **deps**: Bump pypa/gh-action-pypi-publish from 1.13.0 to 1.14.0 in the github-actions group
  ([#10](https://github.com/marcinpsk/netbox-data-import-plugin/pull/10),
  [`5db5d56`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/5db5d564f77a801993c00bb50fce2a27ad43cdac))

- **deps**: Bump the github-actions group across 1 directory with 3 updates
  ([#7](https://github.com/marcinpsk/netbox-data-import-plugin/pull/7),
  [`9ce0b22`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/9ce0b226883ed21a4ddf82afa35411004ab61463))

- **deps**: Bump the github-actions group with 2 updates
  ([#9](https://github.com/marcinpsk/netbox-data-import-plugin/pull/9),
  [`943480d`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/943480d52f05b36a6f1b90dcea226fa9bb85d3c5))


## v1.0.1 (2026-03-04)

### Bug Fixes

- Devcontainer script hardening and refactoring
  ([#1](https://github.com/marcinpsk/netbox-data-import-plugin/pull/1),
  [`4487b76`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/4487b7654be3186331f76ffb782132bbc8827ce0))

### Chores

- **deps**: Bump actions/download-artifact from 4.1.8 to 8.0.0
  ([#2](https://github.com/marcinpsk/netbox-data-import-plugin/pull/2),
  [`c89e5b7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/c89e5b73cd9bdb915e67e9e2e0618cfa2236b684))

- **deps**: Bump actions/upload-artifact from 6.0.0 to 7.0.0
  ([#3](https://github.com/marcinpsk/netbox-data-import-plugin/pull/3),
  [`b61d858`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/b61d858ac05c964a46c27a30055f7906fb3ff415))

- **deps**: Bump astral-sh/setup-uv from 7.3.0 to 7.3.1
  ([#4](https://github.com/marcinpsk/netbox-data-import-plugin/pull/4),
  [`bd637a7`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/bd637a7c77333b914a019456b8f28bd7f271eb5e))

- **deps**: Bump github/codeql-action from 4.32.4 to 4.32.5
  ([#5](https://github.com/marcinpsk/netbox-data-import-plugin/pull/5),
  [`a18dc71`](https://github.com/marcinpsk/netbox-data-import-plugin/commit/a18dc71ff210c220c8a9a7f91d61b2392a7c8f2c))


## v1.0.0 (2026-03-03)

- Initial Release
