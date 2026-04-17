# Entity Types

Controlled vocabulary for wiki entity types. The librarian validates against this
table. Add new types when three or more entities are awkwardly forced into an
existing type (rule of three). New types proposed by the LLM require human approval.

| Type | Description | Example |
|------|------------|---------|
| person | A human being | Alice, Bob |
| company | An organization or business entity | Acme Corp, Initech |
| project | A bounded engagement, initiative, or product | Website redesign, Q4 launch |
| concept | A framework, method, methodology, or idea | Lean startup, Bayesian reasoning |
| tool | Software, service, or physical tool | VS Code, Obsidian, Figma |
| reference | An article, book, paper, video, or external resource | "Thinking Fast and Slow" |
| source | An information source (may overlap with person) | Web article, API doc, meeting notes |
| preference | A personal preference, configuration, or behavioral pattern | Calendar rules, communication style |
| principle | An axiom, guiding rule, or value (can be revised) | "Prioritize honesty", "Ship fast" |
