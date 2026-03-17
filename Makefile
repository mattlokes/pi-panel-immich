PI      := prime
REMOTE  := ~/immich-frame
SESSION := immich-frame
FILES   := immich_frame.py config.yaml

# ── Shared helpers ─────────────────────────────────────────────────────────────
# Find the PID of the python process running in the tmux session.
# Falls back to pgrep if tmux pane PID tree lookup fails.
define GET_PID
	ssh $(PI) '\
		tmux_pid=$$(tmux list-panes -t $(SESSION) -F "#{pane_pid}" 2>/dev/null | head -1); \
		if [ -n "$$tmux_pid" ]; then \
			app_pid=$$(pgrep -P $$tmux_pid python3 2>/dev/null || pgrep -P $$tmux_pid python 2>/dev/null); \
		fi; \
		if [ -z "$$app_pid" ]; then \
			app_pid=$$(pgrep -f immich_frame.py 2>/dev/null); \
		fi; \
		echo $$app_pid'
endef

# ── Targets ────────────────────────────────────────────────────────────────────
.PHONY: deploy run stop restart reload pause resume logs ssh deploy-run status

deploy:
	rsync -av --progress $(FILES) $(PI):$(REMOTE)/
	ssh $(PI) 'chmod +x $(REMOTE)/immich_frame.py'

run:
	ssh $(PI) 'tmux new-session -d -s $(SESSION) \
		"cd $(REMOTE) && WAYLAND_DISPLAY=wayland-1 uv run immich_frame.py 2>&1 | tee -a $(REMOTE)/immich_frame.log"'

stop:
	@pid=$$($(GET_PID)); \
	if [ -z "$$pid" ]; then echo "immich-frame is not running"; exit 1; fi; \
	ssh $(PI) "kill -TERM $$pid"

restart: stop run

reload:
	@pid=$$($(GET_PID)); \
	if [ -z "$$pid" ]; then echo "immich-frame is not running"; exit 1; fi; \
	ssh $(PI) "kill -HUP $$pid"

pause:
	@pid=$$($(GET_PID)); \
	if [ -z "$$pid" ]; then echo "immich-frame is not running"; exit 1; fi; \
	ssh $(PI) "kill -USR1 $$pid"

resume:
	@pid=$$($(GET_PID)); \
	if [ -z "$$pid" ]; then echo "immich-frame is not running"; exit 1; fi; \
	ssh $(PI) "kill -USR2 $$pid"

logs:
	ssh -t $(PI) 'tmux attach-session -t $(SESSION)'

ssh:
	ssh -t $(PI)

deploy-run: deploy restart

status:
	@pid=$$($(GET_PID)); \
	if [ -n "$$pid" ]; then \
		echo "immich-frame is running (PID $$pid)"; \
	else \
		echo "immich-frame is NOT running"; \
	fi
