"""Agent platform foundation (M2-A): validated agent definitions, built-in templates, ownership-scoped custom
agents with immutable versions, and the REST router. Nothing here executes an agent: the execution runtime still
runs only the built-in modules (see ``service.AgentService.resolve`` for the integration point)."""
