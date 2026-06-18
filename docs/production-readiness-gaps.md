# Hyperlane SVM Bridge: Production Readiness Gaps

Assessment of the [hyperlane-demo](../README.md) repository against production requirements for operating a Hyperlane cross-chain token bridge on Solana Virtual Machine (SVM).

## Summary

The hyperlane-demo repo is a local development and testing toolkit. It covers contract deployment, agent setup, and token bridging between two local Solana chains (Gorchain and Local Solana Testnet). It is **not sufficient for production use**. This document catalogues the gaps.

---

## 1. Security

### 1.1 Key Management

**Current state:** Private keys are hardcoded in `README.md` and passed as CLI flags.

**Production requirements:**
- Hardware Security Modules (HSMs) or cloud KMS (AWS KMS, GCP Cloud KMS) for validator and relayer signing keys
- No private keys in source control, documentation, or shell history
- Environment-variable or secrets-manager-based key injection at runtime
- Key rotation procedures with defined rotation schedule
- Separate keys per environment (dev, staging, production)
- Backup and recovery procedures for all key material

### 1.2 Multisig Configuration

**Current state:** Single validator with threshold=1 on each chain.

**Production requirements:**
- m-of-n validator set sizing analysis based on threat model
- Geographic and organizational distribution of validators
- Threshold selection rationale (e.g., 3-of-5, 5-of-9)
- Procedures for adding/removing validators from the set
- Validator key compromise response plan

### 1.3 Network Security

**Current state:** RPC endpoints exposed on localhost with no access controls.

**Production requirements:**
- Firewall rules restricting RPC access to authorized clients
- TLS termination for all RPC endpoints
- Rate limiting on public-facing endpoints
- DDoS mitigation strategy
- Network segmentation between validator, relayer, and RPC nodes
- VPN or private networking between agents and chain RPC providers

### 1.4 Supply Chain Security

**Current state:** `cloud-init.yaml` downloads binaries (Foundry) without checksum verification. Solana CLI installed via unverified install script.

**Production requirements:**
- Checksum verification for all downloaded binaries
- Pinned dependency versions with lock files
- Reproducible build pipeline for Hyperlane programs (.so files)
- Contract audit trail (which commit was built, who built it, build environment hash)

---

## 2. Gas Economics

### 2.1 Broken Gas Enforcement on Sealevel

**Current state:** Documented in `gas.md` — the Sealevel `process_estimate_costs()` function returns hardcoded zeros, making the `OnChainFeeQuoting` relayer enforcement policy non-functional. The relayer accepts any message regardless of gas payment.

**Observed in the explorer:** Enforcement is set to `[{"type": "none"}]` (`stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`), so messages deliver even when the sender paid no interchain gas. A bridged message shows **Status: Delivered** while its *Interchain Gas Payments* panel reads **Total paid: 0** and **Average price: –**: the IGP `GasPayment` event carries the quoted `gas_amount` but a `payment` of `0` (the on-chain IGP quote evaluates to 0 for the route's path). Expected for this bridge — not an explorer/scraper defect.

**Production requirements:**
- Upstream fix to `process_estimate_costs()` in `chains/hyperlane-sealevel/src/mailbox.rs:539` (currently a TODO in Hyperlane codebase)
- Until fixed: use `Minimum` enforcement policy with carefully calculated minimum payment threshold
- Document the risk: without enforcement, the relayer subsidizes all messages, creating a denial-of-service vector (attackers can spam zero-cost messages)
- Cost cap or rate limiting on the relayer to bound subsidy exposure

### 2.2 Gas Oracle Pricing

**Current state:** Static gas oracle values (`token_exchange_rate: 1000000000, gas_price: 1`) configured once via CLI.

**Production requirements:**
- Automated gas oracle update service that periodically fetches real token prices (e.g., SOL/USD from CoinGecko, CoinMarketCap, or on-chain oracles)
- Update frequency analysis (how stale can prices get before users over/underpay unacceptably)
- Price sanity checks (reject oracle updates that deviate > X% from previous value)
- Fallback pricing strategy if price feed is unavailable
- Integration with `getLocalStorageGasOracleConfig()` from Hyperlane TypeScript SDK (referenced in `gas.md` but not implemented)

### 2.3 Cost Analysis

**Current state:** Only deployment cost documented (~5-6 SOL per chain).

**Production requirements:**
- Ongoing operational cost model: validator compute, relayer compute, RPC provider costs, gas subsidies
- Revenue model: fee collection from bridge users
- Break-even analysis
- Reserve requirements for relayer gas funding on each chain

---

## 3. Infrastructure & Operations

### 3.1 Process Management

**Current state:** Agents (validator, relayer) run as foreground processes in terminal sessions.

**Production requirements:**
- systemd unit files or container orchestration (Docker Compose, Kubernetes) for all agents
- Automatic restart on crash with backoff
- Health check endpoints and liveness probes
- Resource limits (CPU, memory, disk) per agent
- Log rotation configuration

### 3.2 High Availability

**Current state:** Single validator, single relayer, no redundancy.

**Production requirements:**
- Multiple validators per chain (already needed for m-of-n multisig)
- Relayer redundancy strategy (active-passive or active-active with deduplication)
- RPC provider failover (primary + fallback endpoints)
- Database replication for validator and relayer RocksDB stores
- Geographic distribution plan

### 3.3 Monitoring & Alerting

**Current state:** No monitoring infrastructure.

**Production requirements:**
- Prometheus metrics collection from validator and relayer (both expose metrics endpoints)
- Grafana dashboards for:
  - Message throughput (dispatched, relayed, failed)
  - Validator checkpoint signing lag
  - Relayer queue depth and delivery latency
  - Gas payment amounts and enforcement outcomes
  - Chain health (block height, RPC latency)
  - Agent uptime and resource usage
- Alerting rules:
  - Validator not signing checkpoints for > N minutes
  - Relayer delivery failures
  - RPC endpoint unreachable
  - Agent process crash
  - Gas oracle prices stale beyond threshold
  - Bridge volume anomalies (potential exploit detection)
- On-call runbook for each alert

### 3.4 Logging

**Current state:** Agent logs go to stdout.

**Production requirements:**
- Centralized log aggregation (ELK stack, Loki, CloudWatch)
- Structured logging with correlation IDs (message IDs)
- Log retention policy
- Log-based alerting for error patterns

### 3.5 Backup & Recovery

**Current state:** No backup procedures documented.

**Production requirements:**
- Validator checkpoint storage backup (currently local filesystem at `./tmp/hyperlane-validator-signatures-*`)
- Relayer and validator RocksDB backup procedures
- Checkpoint storage migration to durable storage (S3, GCS) instead of local filesystem
- Recovery procedure: how to rebuild validator/relayer state from scratch
- Recovery time objective (RTO) and recovery point objective (RPO) definitions

---

## 4. Deployment for Real Chains

### 4.1 Chain Configuration

**Current state:** Only local chains (Gorchain on port 8899, Solana testnet on port 18899).

**Production requirements:**
- Target chain selection and configuration (Solana mainnet, devnet, or other SVM chains)
- Domain ID assignment and registration with Hyperlane registry
- RPC provider selection (Helius, Triton, QuickNode, etc.) with SLA requirements
- Chain-specific configuration (commitment levels, transaction confirmation strategy)
- WebSocket endpoint configuration for real-time event streaming

### 4.2 Contract Deployment

**Current state:** Contracts deployed via CLI with default keypair.

**Production requirements:**
- Multisig-controlled program upgrade authority
- Deployment checklist with pre/post verification steps
- Contract verification against audited source code
- Program freeze plan (make immutable after stability period)
- Deployment to devnet/testnet before mainnet with identical configuration

### 4.3 Integration with Existing Hyperlane Infrastructure

**Current state:** Fully standalone local deployment.

**Production requirements:**
- Strategy for connecting to existing Hyperlane validator sets vs. running standalone
- Chain registry integration for discoverability by other Hyperlane participants
- Interoperability testing with other Hyperlane deployments

---

## 5. Warp Route Operations

### 5.1 Token Supply Management

**Current state:** Arbitrary minting of test USDC with no limits.

**Production requirements:**
- Collateral reserve verification (1:1 backing of synthetics)
- Proof-of-reserves reporting
- Maximum bridge amount per transaction
- Maximum bridge volume per time window
- Collateral rebalancing strategy (if supporting multiple destination chains)

### 5.2 Emergency Controls

**Current state:** No pause or shutdown mechanism documented.

**Production requirements:**
- Emergency pause procedure (halt bridging without losing in-flight messages)
- Circuit breaker triggers (automatic pause on anomalous activity)
- In-flight message handling during pause (complete or refund)
- Communication plan for bridge incidents
- Post-incident recovery and resumption procedure

### 5.3 Rate Limiting & Risk Controls

**Current state:** No rate limiting.

**Production requirements:**
- Per-address rate limits
- Per-token rate limits
- Global volume caps
- Configurable cooldown periods
- Allowlist/blocklist management

---

## 6. Upgrade & Maintenance

### 6.1 Contract Upgrades

**Current state:** No upgrade procedures documented. Solana programs are upgradeable by default.

**Production requirements:**
- Upgrade authority management (multisig or governance)
- Upgrade testing procedure on devnet before mainnet
- Rollback procedure if upgrade causes issues
- User notification and bridge pause during upgrades
- Version tracking and changelog

### 6.2 Agent Upgrades

**Current state:** Agents built from source at a single point in time.

**Production requirements:**
- Agent version pinning and tracking
- Upgrade testing in staging environment
- Rolling upgrade procedure (upgrade validators one at a time)
- Compatibility matrix (which agent versions work with which contract versions)
- Rollback procedure for agent downgrades

### 6.3 Dependency Management

**Current state:** Pinned to Solana CLI v1.18.18 and Foundry nightly-25f24e6 with no upgrade path.

**Production requirements:**
- Dependency update schedule and testing procedure
- Security patch application process
- Compatibility testing when upstream dependencies change

---

## 7. Testing & Validation

### 7.1 Automated Testing

**Current state:** All testing is manual CLI commands with visual verification.

**Production requirements:**
- End-to-end integration test suite (bridge tokens, verify delivery, verify balances)
- Regression test suite run on every config change
- CI/CD pipeline for building and deploying agents and contracts
- Automated smoke tests post-deployment

### 7.2 Load & Stress Testing

**Current state:** No performance testing.

**Production requirements:**
- Throughput benchmarks (messages per second, tokens per minute)
- Latency benchmarks (time from dispatch to delivery)
- Stress testing (sustained high volume)
- Failure mode testing (what happens under 10x expected load)

### 7.3 Chaos Testing

**Current state:** No fault injection testing.

**Production requirements:**
- Validator crash during checkpoint signing
- Relayer crash with messages in queue
- RPC endpoint failure mid-transaction
- Network partition between chains
- Disk full on validator/relayer node
- Clock skew scenarios

---

## Priority Matrix

### P0 — Must fix before production (Critical severity)

- Key management (1.1) — Medium effort
- Broken gas enforcement (2.1) — High effort (requires upstream fix)
- Emergency controls (5.2) — Medium effort
- Multisig configuration (1.2) — Medium effort

### P1 — Required for production (High severity)

- Process management (3.1) — Low effort
- Monitoring and alerting (3.3) — Medium effort
- Real chain deployment (4.1) — Medium effort
- Gas oracle automation (2.2) — Medium effort
- Network security (1.3) — Medium effort
- High availability (3.2) — High effort
- Backup and recovery (3.5) — Medium effort
- Token supply management (5.1) — Medium effort

### P2 — Important for operational maturity (Medium severity)

- Rate limiting (5.3) — Medium effort
- Automated testing (7.1) — High effort
- Contract upgrades (6.1) — Medium effort
- Agent upgrades (6.2) — Low effort
- Cost analysis (2.3) — Low effort
- Logging (3.4) — Low effort

### P3 — Nice to have (Low severity)

- Load testing (7.2) — Medium effort
- Chaos testing (7.3) — High effort
- Full supply chain security — reproducible builds, SBOM, sigstore (1.4) — Low effort (baseline mitigations are in v1 scope)
- Dependency management (6.3) — Low effort
