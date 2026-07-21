package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"time"
)

func main() {
	backendURL := getenv("BACKEND_URL", "http://localhost:9000")
	metricsURL := getenv("METRICS_URL", "")
	listenAddr := getenv("LISTEN_ADDR", ":8080")
	webhookSecret := getenv("GITHUB_WEBHOOK_SECRET", "")

	// 5-minute TTL for delivery ID deduplication
	dedup := newDedupCache(5 * time.Minute)
	// 10 events/minute per repo with a burst cap of 20
	rl := newRateLimiter(10.0/60.0, 20)

	mux := http.NewServeMux()

	mux.HandleFunc("/webhook/github", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		body, err := io.ReadAll(io.LimitReader(r.Body, 5<<20)) // 5 MB cap
		if err != nil {
			http.Error(w, "read error", http.StatusBadRequest)
			return
		}

		// Signature check — this service is the trust boundary
		sig := r.Header.Get("X-Hub-Signature-256")
		if !verifyGitHubSignature(body, webhookSecret, sig) {
			log.Printf("signature mismatch — dropping delivery")
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}

		// Deduplication: drop retries GitHub sends on non-2xx responses
		deliveryID := r.Header.Get("X-GitHub-Delivery")
		if deliveryID != "" && dedup.seen(deliveryID) {
			log.Printf("duplicate delivery %s — dropping", deliveryID)
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"status": "duplicate"})
			return
		}

		// Rate limit per repo to protect against flooding
		var payload map[string]any
		if err := json.Unmarshal(body, &payload); err == nil {
			if repo, ok := repoFullName(payload); ok && !rl.allow(repo) {
				log.Printf("rate limit exceeded for %s", repo)
				http.Error(w, "too many requests", http.StatusTooManyRequests)
				return
			}
		}

		// Fan out concurrently to backend + optional metrics sink
		targets := []string{backendURL + "/webhook/github"}
		if metricsURL != "" {
			targets = append(targets, metricsURL)
		}
		headers := map[string]string{
			"Content-Type":      "application/json",
			"X-GitHub-Event":    r.Header.Get("X-GitHub-Event"),
			"X-GitHub-Delivery": deliveryID,
		}
		fanOut(targets, body, headers, 10*time.Second)

		w.WriteHeader(http.StatusAccepted)
		json.NewEncoder(w).Encode(map[string]string{"status": "accepted"})
	})

	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	})

	log.Printf("ingest listening on %s → backend %s", listenAddr, backendURL)
	if err := http.ListenAndServe(listenAddr, mux); err != nil {
		log.Fatal(err)
	}
}

func repoFullName(payload map[string]any) (string, bool) {
	repo, ok := payload["repository"].(map[string]any)
	if !ok {
		return "", false
	}
	name, ok := repo["full_name"].(string)
	return name, ok
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
