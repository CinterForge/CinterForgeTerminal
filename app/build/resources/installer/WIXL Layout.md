# How to Write `wixl` Installer Layouts (For HTML/CSS Designers)

In `wixl`, there is no CSS file, no flexbox, and no DOM nesting for layout. Think of a `wixl` UI file as **an SVG canvas with inline attributes**—every dialog is a fixed artboard, and every UI widget is a flat `<Control>` tag positioned with absolute `(X, Y, Width, Height)` coordinates.

---

## 1. Mental Model: HTML/CSS vs. `wixl` XML

| Concept | HTML / CSS | `wixl` XML |
| :--- | :--- | :--- |
| **Page / Screen** | `<div>` or `<section>` | `<Dialog Id="Page1" Width="370" Height="270">` |
| **Layout Engine** | Flexbox / Grid / Flow | **Absolute positioning only** (`X`, `Y`, `Width`, `Height`) |
| **Units** | `px`, `rem`, `%` | **Dialog Units (DU)** (`1 DU ≈ 1.5px` wide × `1.6px` tall; no `%` or `auto`) |
| **CSS Classes** | `.title { font-size: 14px; }` | `<TextStyle Id="TitleFont" ... />` prepended as `{\TitleFont}Text` |
| **Background Color** | `background: #fff;` | **Not supported on dialogs.** You layer a white `<Control Type="Bitmap">` underneath. |
| **Z-Index / Stacking** | `z-index: 10;` | **Source order** (later tags draw on top), plus `TabSkip="yes"` on background images |
| **Click Handlers** | `onclick="goNext()"` | `<Publish Event="NewDialog" Value="Page2">1</Publish>` inside a button |

---

## 2. Anatomy of the UI File (`ui.wxs`)

Everything lives inside a single `<UI>` block. Here is the skeleton you will edit:

```xml
<UI>
  <!-- 1. FONT DEFINITIONS (Like your CSS @font-face / utility classes) -->
  <!-- Required: If DefaultUIFont is missing, the installer crashes on launch -->
  <Property Id="DefaultUIFont" Value="BodyFont" />
  <TextStyle Id="BodyFont" FaceName="Segoe UI" Size="8" />
  <TextStyle Id="HeaderFont" FaceName="Segoe UI" Size="11" Bold="yes" />
  <TextStyle Id="MutedFont" FaceName="Segoe UI" Size="8" Red="110" Green="110" Blue="110" />

  <!-- 2. IMAGE ASSETS (Declared here, placed as <Control Type="Bitmap"> below) -->
  <Binary Id="BannerBmp" SourceFile="assets/banner.bmp" />

  <!-- 3. SCREENS (Each <Dialog> is one modal step in the wizard) -->
  <Dialog Id="WelcomeDlg" Width="370" Height="270" Title="[ProductName] Setup">
    <!-- Controls go here -->
  </Dialog>

  <Dialog Id="OptionsDlg" Width="370" Height="270" Title="[ProductName] Setup">
    <!-- Controls go here -->
  </Dialog>

  <!-- 4. ENTRY POINT (Which <Dialog> opens first when the user double-clicks the .msi) -->
  <InstallUISequence>
    <Show Dialog="WelcomeDlg" Before="ExecuteAction">NOT Installed</Show>
  </InstallUISequence>
</UI>
```

---

## 3. How to Position & Style Elements

Every visual element inside a `<Dialog>` is a `<Control>` tag. There is **no nesting**—buttons, text, and backgrounds are all siblings.

### A. Adding Text & Applying Fonts

There is no `class=""` attribute. To style text, prefix the `Text` string with `{\StyleId}`:

```xml
<!-- Uses DefaultUIFont automatically -->
<Control Id="Desc" Type="Text" X="20" Y="60" Width="330" Height="30"
         Transparent="yes" NoPrefix="yes"
         Text="This will install the application on your computer." />

<!-- Uses HeaderFont via inline prefix -->
<Control Id="Title" Type="Text" X="20" Y="12" Width="300" Height="18"
         Transparent="yes" NoPrefix="yes"
         Text="{\HeaderFont}Choose Installation Options" />
```

* **Gotcha 1 (`Transparent="yes"`):** Always add `Transparent="yes"` to `<Control Type="Text">`. Without it, Windows renders a solid gray rectangle behind your text even if it sits on a white background.
* **Gotcha 2 (`NoPrefix="yes"`):** Windows treats `&` as an underline hotkey shortcut unless you add `NoPrefix="yes"`.
* **Gotcha 3 (Height clipping):** Text wraps automatically at `Width`, and **clips silently** if it exceeds `Height`. Always give multi-line text extra vertical room (`~12 DU` per line).

### B. Layering Backgrounds & Images (Z-Index)

Because `<Dialog>` has no background color property, white headers or full white welcome screens are made by placing a `.bmp` image first in the XML and putting text controls after it:

```xml
<!-- 1. Placed FIRST so it renders behind text; TabSkip="yes" prevents focus box -->
<Control Id="TopWhiteBg" Type="Bitmap" X="0" Y="0" Width="370" Height="44"
         TabSkip="yes" Text="BannerBmp" />

<!-- 2. Placed AFTER so it renders on top of the bitmap -->
<Control Id="HeaderTitle" Type="Text" X="15" Y="10" Width="280" Height="16"
         Transparent="yes" NoPrefix="yes" Text="{\HeaderFont}Setup Options" />

<!-- 3. 1px-style horizontal divider line -->
<Control Id="HeaderLine" Type="Line" X="0" Y="44" Width="370" Height="0" />
```

### C. Interactive Form Controls & State Binding

In MSI, form inputs don't use `name=""`—they bind directly to uppercase **MSI Properties** (global state variables).

```xml
<!-- Checkbox: Sets CREATE_DESKTOP_ICON="1" when checked, or null when unchecked -->
<Control Id="DesktopCheck" Type="CheckBox" X="20" Y="70" Width="250" Height="16"
         Property="CREATE_DESKTOP_ICON" CheckBoxValue="1"
         Text="Create a Desktop shortcut" />

<!-- Text Input: Binds user input to the API_KEY property -->
<Control Id="ApiLabel" Type="Text" X="20" Y="100" Width="200" Height="12"
         Transparent="yes" Text="API Key (Optional):" />
<Control Id="ApiInput" Type="Edit" X="20" Y="114" Width="220" Height="16"
         Property="API_KEY" />

<!-- Radio Group: Binds selection to INSTALL_SCOPE -->
<Control Id="ScopeGroup" Type="RadioButtonGroup" X="20" Y="145" Width="300" Height="45"
         Property="INSTALL_SCOPE">
  <RadioButton Value="user" X="0" Y="0" Width="280" Height="16" Text="Install just for me" />
  <RadioButton Value="all"  X="0" Y="20" Width="280" Height="16" Text="Install for all users" />
</Control>
```

---

## 4. Wiring Navigation Between Screens (`<Publish>`)

Buttons use `<Publish>` tags instead of JavaScript event listeners. The inner text `1` is a boolean condition (`1` = always true):

```xml
<!-- "Back" Button: Jumps to previous <Dialog> -->
<Control Id="BackBtn" Type="PushButton" X="180" Y="243" Width="56" Height="17" Text="Back">
  <Publish Event="NewDialog" Value="WelcomeDlg">1</Publish>
</Control>

<!-- "Next" Button: Jumps to next <Dialog> (Default="yes" binds the Enter key) -->
<Control Id="NextBtn" Type="PushButton" X="236" Y="243" Width="56" Height="17"
         Default="yes" Text="Next">
  <Publish Event="NewDialog" Value="OptionsDlg">1</Publish>
</Control>

<!-- "Install" Button (On the FINAL screen): Closes UI and starts file installation -->
<Control Id="InstallBtn" Type="PushButton" X="236" Y="243" Width="56" Height="17"
         Default="yes" Text="Install">
  <Publish Event="EndDialog" Value="Return">1</Publish>
</Control>

<!-- "Cancel" Button: Aborts installer (Cancel="yes" binds the Esc key) -->
<Control Id="CancelBtn" Type="PushButton" X="304" Y="243" Width="56" Height="17"
         Cancel="yes" Text="Cancel">
  <Publish Event="EndDialog" Value="Exit">1</Publish>
</Control>
```

### Conditional Show/Hide & Disable (Like CSS `display: none` or `:disabled`)

You can show, hide, or disable any control dynamically based on a property using `<Condition>`:

```xml
<Control Id="ApiInput" Type="Edit" X="20" Y="114" Width="220" Height="16" Property="API_KEY">
  <!-- Disable input unless checkbox is checked -->
  <Condition Action="disable">NOT ENABLE_API</Condition>
  <Condition Action="enable">ENABLE_API</Condition>
</Control>
```

---

## 5. Build & Live-Preview Loop on Linux

You don't need to build the whole application to preview your UI changes. Compile a dummy `.msi` with `wixl` and open it in **Wine** (on Linux/macOS) or Windows:

```bash
# 1. Compile XML to .msi
wixl -o preview.msi main.wxs ui.wxs

# 2. Launch UI preview (on Linux with Wine)
wine msiexec /i preview.msi

# 3. If the UI fails to open, dump the debug log to see exact line/control errors
wine msiexec /i preview.msi /l*v ui.log && grep -i "error\|28" ui.log
```

---

## 6. Top 5 Gotchas for CSS Developers

1. **No Flex/Margin Auto:** To center an element of width `W` on a `370`-wide dialog, calculate `X = (370 - W) / 2` manually.
2. **Coordinate Overflow Crashes:** If `X + Width > 370` or `Y + Height > 270`, Windows Installer throws runtime error `2826` and refuses to render the dialog.
3. **Only `.bmp` and `.ico` Images:** PNG, JPG, and SVG will fail to compile or render blank. Export `.bmp` files as **24-bit RGB without color-space metadata**.
4. **No Inline Spans:** You cannot make *one word* bold inside a sentence (`Hello <b>World</b>` will print literal `<b>` tags). Split it into two side-by-side `<Control Type="Text">` tags if you need mixed weights.
5. **Required Boilerplate:** Every project needs `DefaultUIFont` defined and at least one `<Publish Event="EndDialog" Value="Return">1</Publish>` button—otherwise the user can never actually trigger the installation.
