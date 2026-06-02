# Adding Custom Keyboard Layouts

The `win-search-aliases` tool allows you to add custom keyboard layouts if your preferred layout is not supported out of the box.

Keyboard mappings are defined in a TOML configuration file located at:
[`src/win_search_aliases/config/keyboard_maps.toml`](../src/win_search_aliases/config/keyboard_maps.toml)

## How to add your layout

1. Open the `keyboard_maps.toml` file in any text editor.
2. Add a new section for your layout under the `[maps]` table. 
3. Provide a unique ID for your layout (e.g., `es-qwerty`).
4. Add a `description` for clarity.
5. Define the `mapping` dictionary where the keys are the English (US QWERTY) characters and the values are the corresponding characters in your layout.

### Example

If you wanted to add a hypothetical layout, it would look like this:

```toml
[maps."my-custom-layout"]
description = "My custom keyboard layout typed from a US QWERTY keyboard"
mapping = { q = "1", w = "2", e = "3", r = "4", t = "5", y = "6", u = "7", i = "8", o = "9", p = "0", a = "a", s = "b", d = "c", f = "d", g = "e", h = "f", j = "g", k = "h", l = "i", z = "j", x = "k", c = "l", v = "m", b = "n", n = "o", m = "p" }
```

## Enabling Auto-Detection (Optional)

If you want the `win-search-aliases auto` command to automatically detect and apply your new layout when it is active in Windows, you must also map it in the `profiles.toml` file located at:
[`src/win_search_aliases/config/profiles.toml`](../src/win_search_aliases/config/profiles.toml)

Add your layout ID as a key, and provide an array of Windows Language/Keyboard Profile IDs as the value. You can find the list of Windows keyboard identifiers [here](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-language-pack-default-values).

```toml
[profiles]
# Example: Mapping "my-custom-layout" to Spanish (Spain) and Spanish (Mexico)
"my-custom-layout" = ["00000C0A", "0000080A"]
```

## Contributing your layout

If you add a standard layout that could be useful to others (e.g., German QWERTZ, French AZERTY, etc.), please consider contributing it back to the project!

1. Fork the repository.
2. Add your layout to `keyboard_maps.toml`.
3. Create a Pull Request (PR) with a brief explanation of the added layout.
