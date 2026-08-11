# Dummy camera assets

These four fixed 656 x 492 experimental STO frames make the GUI camera dummy
presets self-contained. `DummyCamera` maps each preset to one explicit file;
it does not select arbitrary images from an external research-data checkout.

| Preset | File | SHA-256 |
| --- | --- | --- |
| `dummy` | `1x1_1.bmp` | `c99f26377b19306a449874fdc062a2e58bc31f27c11388d116f709acd95b6657` |
| `dummy_c6x2` | `c6x2_1.bmp` | `cf059f8530f0aee93df928425d498a0aa96e34f4303ee1ba6664c579e263c8c1` |
| `dummy_tw` | `Twinned2x1_1.bmp` | `cb9b108583dabc19a59c8b62c264d2345e84afa2b5c9b789fabbaf36db7bcfac` |
| `dummy_rt13_tilted` | `RT13_20.png` | `322004dbb7324cfc85066a671857d1860098b26bcda6afd2558f97f58c26ba6d` |

The RT13 preset applies the existing 15-degree display rotation at runtime.
If an asset is missing or corrupt, dummy camera connection fails explicitly;
there is no generated bright-spot fallback.
