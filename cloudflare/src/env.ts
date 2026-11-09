export interface Env {
  NEXUS_HUB: DurableObjectNamespace;
  SCOUT_PAIR_TOKEN: string;
  MCP_BEARER_TOKEN: string;
}

export function hubStub(env: Env): DurableObjectStub {
  return env.NEXUS_HUB.get(env.NEXUS_HUB.idFromName("default"));
}
