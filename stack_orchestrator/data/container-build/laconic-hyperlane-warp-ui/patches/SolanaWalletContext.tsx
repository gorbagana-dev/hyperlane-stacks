import { SnapWalletAdapter } from '@drift-labs/snap-wallet-adapter';
import { WalletError } from '@solana/wallet-adapter-base';
import { ConnectionProvider, WalletProvider } from '@solana/wallet-adapter-react';
import { WalletModalProvider } from '@solana/wallet-adapter-react-ui';
import '@solana/wallet-adapter-react-ui/styles.css';
import {
  LedgerWalletAdapter,
  SalmonWalletAdapter,
  SolflareWalletAdapter,
  TrustWalletAdapter,
} from '@solana/wallet-adapter-wallets';
import { PropsWithChildren, useCallback, useMemo } from 'react';
import { toast } from 'react-toastify';
import { logger } from '../../../utils/logger';

// Fixed RPC endpoint for wallet initialization.
// Sentinel value replaced at container start by entrypoint.sh.
//
// The ConnectionProvider endpoint is used for wallet autoConnect and the
// useSolanaActiveChain hook. It does NOT need to match the origin chain —
// the actual transaction code (solana.ts) creates per-chain Connection
// objects via multiProvider.getRpcUrl(chainName).
//
// Using a fixed endpoint avoids the autoConnect failure that occurs when
// the ConnectionProvider points to a different chain's RPC than what the
// wallet adapter expects (e.g. reverse bridge: origin=gorchain but wallet
// was initialized on solana).
const WALLET_ENDPOINT = '__SOLANA_RPC_URL__';

export function SolanaWalletContext({ children }: PropsWithChildren<unknown>) {
  const endpoint = useMemo(() => WALLET_ENDPOINT, []);

  const wallets = useMemo(
    () => [
      new SolflareWalletAdapter(),
      new SalmonWalletAdapter(),
      new SnapWalletAdapter(),
      new TrustWalletAdapter(),
      new LedgerWalletAdapter(),
    ],
    [],
  );

  const onError = useCallback((error: WalletError) => {
    logger.error('Error initializing Solana wallet provider', error);
    toast.error('Error preparing Solana wallet');
  }, []);

  return (
    <ConnectionProvider endpoint={endpoint}>
      <WalletProvider wallets={wallets} onError={onError} autoConnect>
        <WalletModalProvider>{children}</WalletModalProvider>
      </WalletProvider>
    </ConnectionProvider>
  );
}
