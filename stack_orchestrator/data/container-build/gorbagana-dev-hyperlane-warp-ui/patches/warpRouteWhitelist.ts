// Keep whitelist null so all routes (registry + custom YAML) are available.
// Our custom gorchain/solana tokens from warpRoutes.yaml are merged alongside
// published registry routes. The UI defaults to showing registry routes but
// our tokens are available via the token selector.
export const warpRouteWhitelist: Array<string> | null = null;
