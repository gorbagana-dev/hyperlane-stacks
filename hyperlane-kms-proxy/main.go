package main

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/sha256"
	"encoding/asn1"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math/big"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/btcsuite/btcd/btcec/v2"
	btcecdsa "github.com/btcsuite/btcd/btcec/v2/ecdsa"
	"golang.org/x/crypto/sha3"
)

var (
	privyAppID     string
	privyAppSecret string
	privyWalletID  string
	privyAPIURL    string
	publicKey      *ecdsa.PublicKey
	httpClient     = &http.Client{Timeout: 30 * time.Second}
)

// --- Privy API ---

type privyRPCRequest struct {
	ChainType string      `json:"chain_type"`
	Method    string      `json:"method"`
	Params    interface{} `json:"params"`
}

type privyRPCResponse struct {
	Method string `json:"method"`
	Data   struct {
		Signature string `json:"signature"`
		Encoding  string `json:"encoding"`
	} `json:"data"`
}

func privySign(digest []byte) ([]byte, error) {
	body, _ := json.Marshal(privyRPCRequest{
		ChainType: "ethereum",
		Method:    "secp256k1_sign",
		Params:    map[string]string{"hash": "0x" + hex.EncodeToString(digest), "encoding": "hex"},
	})

	url := fmt.Sprintf("%s/v1/wallets/%s/rpc", privyAPIURL, privyWalletID)
	req, _ := http.NewRequest("POST", url, bytes.NewReader(body))
	req.SetBasicAuth(privyAppID, privyAppSecret)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("privy-app-id", privyAppID)

	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("privy request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("privy API error %d: %s", resp.StatusCode, string(b))
	}

	var privyResp privyRPCResponse
	if err := json.NewDecoder(resp.Body).Decode(&privyResp); err != nil {
		return nil, fmt.Errorf("failed to decode privy response: %w", err)
	}

	sig := strings.TrimPrefix(privyResp.Data.Signature, "0x")
	return hex.DecodeString(sig)
}

// --- DER encoding ---

type ecdsaSig struct {
	R, S *big.Int
}

func sigToDER(sig []byte) ([]byte, error) {
	if len(sig) < 64 {
		return nil, fmt.Errorf("signature too short: %d bytes", len(sig))
	}
	return asn1.Marshal(ecdsaSig{
		R: new(big.Int).SetBytes(sig[:32]),
		S: new(big.Int).SetBytes(sig[32:64]),
	})
}

// SubjectPublicKeyInfo DER encoding for secp256k1 public key.
func pubKeyToSPKI(pub *ecdsa.PublicKey) ([]byte, error) {
	uncompressed := elliptic.Marshal(pub.Curve, pub.X, pub.Y)
	type algID struct {
		Algorithm  asn1.ObjectIdentifier
		Parameters asn1.ObjectIdentifier
	}
	type spki struct {
		Algorithm algID
		PublicKey asn1.BitString
	}
	return asn1.Marshal(spki{
		Algorithm: algID{
			Algorithm:  asn1.ObjectIdentifier{1, 2, 840, 10045, 2, 1}, // id-ecPublicKey
			Parameters: asn1.ObjectIdentifier{1, 3, 132, 0, 10},       // secp256k1
		},
		PublicKey: asn1.BitString{Bytes: uncompressed, BitLength: len(uncompressed) * 8},
	})
}

// --- KMS request/response types ---

type kmsRequest struct {
	KeyId            string `json:"KeyId"`
	Message          string `json:"Message"`
	MessageType      string `json:"MessageType"`
	SigningAlgorithm string `json:"SigningAlgorithm"`
}

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/x-amz-json-1.1")
	json.NewEncoder(w).Encode(v)
}

func writeKMSError(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("Content-Type", "application/x-amz-json-1.1")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"__type": code, "message": msg})
}

// --- KMS handlers ---

func handleSign(w http.ResponseWriter, body []byte) {
	var req kmsRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeKMSError(w, 400, "InvalidParameterValue", "invalid request body")
		return
	}

	msg, err := base64.StdEncoding.DecodeString(req.Message)
	if err != nil {
		writeKMSError(w, 400, "InvalidParameterValue", "invalid base64 in Message")
		return
	}

	// If RAW, hash with SHA-256 first. If DIGEST, use as-is.
	digest := msg
	if req.MessageType != "DIGEST" {
		h := sha256.Sum256(msg)
		digest = h[:]
	}

	sig, err := privySign(digest)
	if err != nil {
		log.Printf("Sign error: %v", err)
		writeKMSError(w, 500, "KMSInternalException", "signing failed")
		return
	}

	der, err := sigToDER(sig)
	if err != nil {
		log.Printf("DER encoding error: %v", err)
		writeKMSError(w, 500, "KMSInternalException", "signature encoding failed")
		return
	}

	writeJSON(w, map[string]interface{}{
		"KeyId":            req.KeyId,
		"Signature":        base64.StdEncoding.EncodeToString(der),
		"SigningAlgorithm": "ECDSA_SHA_256",
	})
}

func handleGetPublicKey(w http.ResponseWriter, body []byte) {
	var req kmsRequest
	json.Unmarshal(body, &req)

	spki, err := pubKeyToSPKI(publicKey)
	if err != nil {
		writeKMSError(w, 500, "KMSInternalException", "public key encoding failed")
		return
	}

	writeJSON(w, map[string]interface{}{
		"KeyId":             req.KeyId,
		"PublicKey":         base64.StdEncoding.EncodeToString(spki),
		"KeyUsage":          "SIGN_VERIFY",
		"KeySpec":           "ECC_SECG_P256K1",
		"SigningAlgorithms": []string{"ECDSA_SHA_256"},
	})
}

func handleDescribeKey(w http.ResponseWriter, body []byte) {
	var req kmsRequest
	json.Unmarshal(body, &req)

	writeJSON(w, map[string]interface{}{
		"KeyMetadata": map[string]interface{}{
			"KeyId":             req.KeyId,
			"Arn":               fmt.Sprintf("arn:aws:kms:us-east-1:000000000000:key/%s", req.KeyId),
			"KeyState":          "Enabled",
			"KeyUsage":          "SIGN_VERIFY",
			"KeySpec":           "ECC_SECG_P256K1",
			"Enabled":           true,
			"KeyManager":        "CUSTOMER",
			"Origin":            "EXTERNAL",
			"SigningAlgorithms":  []string{"ECDSA_SHA_256"},
		},
	})
}

// --- Router ---

func handler(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/health" {
		w.WriteHeader(200)
		w.Write([]byte("ok"))
		return
	}

	if r.Method != "POST" {
		writeKMSError(w, 405, "MethodNotAllowed", "only POST is supported")
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeKMSError(w, 400, "InvalidParameterValue", "failed to read body")
		return
	}

	target := r.Header.Get("X-Amz-Target")
	switch target {
	case "TrentService.Sign":
		handleSign(w, body)
	case "TrentService.GetPublicKey":
		handleGetPublicKey(w, body)
	case "TrentService.DescribeKey":
		handleDescribeKey(w, body)
	default:
		writeKMSError(w, 400, "UnsupportedOperation", fmt.Sprintf("unsupported target: %s", target))
	}
}

// --- Startup: recover public key ---

func recoverPublicKey() (*ecdsa.PublicKey, error) {
	hash := sha256.Sum256([]byte("kms-proxy-pubkey-recovery"))
	sig, err := privySign(hash[:])
	if err != nil {
		return nil, fmt.Errorf("recovery sign failed: %w", err)
	}

	if len(sig) < 65 {
		return nil, fmt.Errorf("expected 65-byte signature for recovery, got %d", len(sig))
	}

	// Normalize v: Ethereum uses 27/28, btcec RecoverCompact expects 27/28.
	v := sig[64]
	if v < 27 {
		v += 27
	}
	// RecoverCompact format: [v(1) || r(32) || s(32)]
	compactSig := make([]byte, 65)
	compactSig[0] = v
	copy(compactSig[1:33], sig[:32])
	copy(compactSig[33:65], sig[32:64])

	pub, _, err := btcecdsa.RecoverCompact(compactSig, hash[:])
	if err != nil {
		return nil, fmt.Errorf("ecrecover failed: %w", err)
	}

	// Convert btcec public key to ecdsa public key
	ecdsaPub := pub.ToECDSA()

	// Log Ethereum address for verification
	uncompressed := elliptic.Marshal(btcec.S256(), ecdsaPub.X, ecdsaPub.Y)
	addrHash := sha3.NewLegacyKeccak256()
	addrHash.Write(uncompressed[1:]) // skip 0x04 prefix
	addr := addrHash.Sum(nil)[12:]
	log.Printf("Recovered validator public key, Ethereum address: 0x%s", hex.EncodeToString(addr))

	return ecdsaPub, nil
}

func main() {
	privyAppID = os.Getenv("PRIVY_APP_ID")
	privyAppSecret = os.Getenv("PRIVY_APP_SECRET")
	privyWalletID = os.Getenv("PRIVY_WALLET_ID")
	privyAPIURL = os.Getenv("PRIVY_API_URL")
	if privyAPIURL == "" {
		privyAPIURL = "https://api.privy.io"
	}

	if privyAppID == "" || privyAppSecret == "" || privyWalletID == "" {
		log.Fatal("PRIVY_APP_ID, PRIVY_APP_SECRET, and PRIVY_WALLET_ID are required")
	}

	listenAddr := os.Getenv("LISTEN_ADDR")
	if listenAddr == "" {
		listenAddr = ":9999"
	}

	var err error
	publicKey, err = recoverPublicKey()
	if err != nil {
		log.Fatalf("Failed to recover public key: %v", err)
	}

	http.HandleFunc("/", handler)
	log.Printf("KMS proxy listening on %s (wallet: %s)", listenAddr, privyWalletID)
	log.Fatal(http.ListenAndServe(listenAddr, nil))
}
