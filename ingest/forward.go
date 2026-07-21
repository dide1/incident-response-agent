package main

import (
	"bytes"
	"context"
	"log"
	"net/http"
	"time"
)

// fanOut sends body to every target URL concurrently via goroutines.
// Each goroutine is independent: a slow or failing target doesn't block the others.
func fanOut(targets []string, body []byte, headers map[string]string, timeout time.Duration) {
	for _, url := range targets {
		go func(url string) {
			ctx, cancel := context.WithTimeout(context.Background(), timeout)
			defer cancel()

			req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
			if err != nil {
				log.Printf("fanOut: build request failed for %s: %v", url, err)
				return
			}
			for k, v := range headers {
				req.Header.Set(k, v)
			}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				log.Printf("fanOut: request to %s failed: %v", url, err)
				return
			}
			resp.Body.Close()
			log.Printf("fanOut: %s → %d", url, resp.StatusCode)
		}(url)
	}
}
