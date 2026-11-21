// This test is compiled alongside the pinned upstream's internal/app test package.
package app_test

import (
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"testing"
	"time"

	sdk "github.com/coder/acp-go-sdk"
	"github.com/sujoshua/agy-gateway/internal/acptest"
	"github.com/sujoshua/agy-gateway/internal/config"
)

func TestCCFleetLocalWorkspace(t *testing.T) {
	e := newEnv(t, func(c *config.Config) {
		c.IgnoreUnsupported = os.Getenv("FIXTURE_COMPAT") == "1"
	})
	file := os.Getenv("FIXTURE_FILE")
	shared := os.Getenv("FIXTURE_SHARED")
	if _, err := os.ReadFile(file); !errors.Is(err, os.ErrPermission) {
		t.Fatal("gateway must be denied direct access to the agent's project")
	}
	marker := regexp.MustCompile(`INPUT_[0-9a-f]{32}`)
	e.agent.OnPrompt(func(turn *acptest.Turn) (sdk.StopReason, error) {
		result, err := turn.Call("read_file", map[string]any{"Path": file})
		if err != nil {
			return "", err
		}
		original := marker.FindString(result.Text)
		if original == "" {
			return "", errors.New("client's file contents missing from tool result")
		}
		if err := os.WriteFile(filepath.Join(shared, "read"), []byte(original), 0600); err != nil {
			return "", err
		}
		_, err = turn.Call("edit_file", map[string]any{
			"Path": file, "Target": original, "Replacement": "Edited by the local agent.",
		})
		if err != nil {
			return "", err
		}
		return sdk.StopReasonEndTurn, turn.Say("LOCAL_EDIT_COMPLETE")
	})
	if err := os.WriteFile(filepath.Join(shared, "url.tmp"), []byte(e.url), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(shared, "url.tmp"), filepath.Join(shared, "url")); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(50 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(filepath.Join(shared, "done")); err == nil {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("timed out waiting for the native client")
}
