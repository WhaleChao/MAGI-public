# Obsidian Flavored Markdown Format Reference

Source: kepano/obsidian-skills (Official Obsidian Agent Skills)

## Internal Links (Wikilinks)

- `[[Note Name]]` - basic link
- `[[Note Name|Display Text]]` - custom display text
- `[[Note Name#Heading]]` - link to heading
- `[[Note Name#^block-id]]` - link to block reference
- `[[#Heading in same note]]` - same-note heading link

Block IDs: append `^block-id` to any paragraph to make it referenceable.

## Embeds

Prefix wikilinks with `!` to embed:
- `![[Note Name]]` - embed full note
- `![[Note Name#Heading]]` - embed section
- `![[image.png]]` or `![[image.png|300]]` - image with optional width
- `![[document.pdf#page=3]]` - specific PDF page

## Callouts

```markdown
> [!type] Optional Title
> Content here
```

Types: note, tip, warning, info, example, quote, bug, danger, success, failure, question, abstract, todo

Collapsible: `> [!type]-` (collapsed) or `> [!type]+` (expanded)

## Properties (Frontmatter)

```yaml
---
tags:
  - tag1
  - nested/tag
aliases:
  - alternate name
cssclasses:
  - custom-class
---
```

## Tags

Format: `#tag` or `#nested/tag`
- Letters, numbers (not first char), underscores, hyphens, forward slashes
- Minimum 1 non-numeric character

## Additional Syntax

- Comments: `%%hidden text%%`
- Highlighting: `==highlighted text==`
- Math (LaTeX): inline `$equation$`, block `$$equation$$`
- Diagrams: Mermaid code blocks
- Footnotes: `[^1]` with `[^1]: definition`

## obsidian-cli Commands

When obsidian-cli is available:
- `obsidian-cli search "query"` - search note names
- `obsidian-cli search-content "query"` - search inside notes
- `obsidian-cli create "Folder/Note" --content "..."` - create note
- `obsidian-cli move "old/path" "new/path"` - move/rename (updates wikilinks)
- `obsidian-cli delete "path"` - delete note
- `obsidian-cli set-default "vault-name"` - set default vault
- `obsidian-cli print-default --path-only` - show active vault path
