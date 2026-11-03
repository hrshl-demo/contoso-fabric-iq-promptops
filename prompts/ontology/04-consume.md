---
id: ontology.consume
title: Ask the ontology business questions and check the answers
version: 1.0.0
area: ontology
stage: consume
skills: [fabriciq-ontology-cli]
inputs:
  workspace: Contoso-Bank-Ontology-Demo
  date: today, YYYY-MM-DD
outputs:
  - docs/consume/**
guardrails: [pr-only, preview-before-deploy, synthetic-data-only]
---
Plan first and show me the plan. Then use the ontology agent and the graph in the workspace `{{workspace}}` to answer business questions about the Contoso retail bank, and check the answers against the golden set. This is read-only.

1. Ask the questions in `kit/model/golden.yaml` that carry the `smoke` tag (a mandate cap, the primary account balance, the home-loan EMI, the highest fixed-deposit rate, the Classic Credit Card annual fee).
2. For each answer, show the question, the answer, the entities and relationships used, and whether it matches the golden answer. Report mismatches. Never edit the golden answers to make a result pass.
3. Add three questions a relationship manager would ask that need a relationship path, for example which household members may see a customer's loan details and who granted that, and show the graph query behind each answer.

Write the results to `docs/consume/{{date}}.md`. Do not change the ontology, the graph or any data. Open a pull request that contains only that file, and quote this prompt's id and version in it.
