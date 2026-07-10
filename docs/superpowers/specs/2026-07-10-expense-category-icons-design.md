# Expense Category Icons Design

## Goal

Persist each expense's category and chosen icon so every ledger member and device sees the same visual identity. Presets automatically supply their category and icon, while users may choose from curated SF Symbols or a single Emoji.

## Data Contract

Expenses gain nullable `category`, `icon_type`, and `icon_value` fields. `icon_type` is either `sf_symbol` or `emoji`; `icon_value` is required when a type is present. Existing rows remain null and clients render `yensign.circle.fill`.

The create and read schemas expose all three fields. The backend accepts curated SF Symbol identifiers and one visible Emoji character, rejects inconsistent pairs, and limits category/value lengths. A nullable migration avoids guessing historical categories.

## iOS Experience

The existing preset catalog becomes a shared category catalog. Selecting a preset fills the title, category, and preset icon. A new icon control opens a compact picker with curated SF Symbols and Emoji. The current selection appears in the creation header and the saved expense row. Voice drafts map known categories to a default catalog icon. Editing restores saved values.

## Compatibility and Testing

Old servers/rows decode through optional fields. Backend tests cover persistence, validation, and null compatibility. iOS tests cover API encoding/decoding and catalog lookup. Full backend pytest and iOS xcodebuild tests are required.
