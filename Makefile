# Test entry points (docs/TESTING.md). Everything goes through scripts/test.py (Python 3 stdlib only).
#   make test-unit            pure logic, no DB / network / docker
#   make test-functional      TestClient + throwaway MySQL schema (TEST_DB_NAME, default vtest)
#   make test-system          live-stack BRD suites (reports_platform always last)
#   make test-e2e             journeys J1-J6 (needs: make test-env-up-e2e)
#   make test-ui              Playwright suites, one after another
#   make test-all             every layer in order
# Pass runner options with ARGS, e.g.  make test-system ARGS="--suite contacts --only CM-00"
#                                      make test-all ARGS="--restart-api-between"

PYTHON ?= python3
RUNNER := $(PYTHON) scripts/test.py
ARGS ?=

.PHONY: test-unit test-functional test-system test-e2e test-ui test-all test-list test-doctor \
        test-dry-run test-report test-collect test-trace test-db-create test-db-drop test-db-reset \
        test-env-up test-env-down test-env-up-e2e test-env-down-e2e

test-unit:
	$(RUNNER) run unit $(ARGS)

test-functional:
	$(RUNNER) run functional $(ARGS)

test-system:
	$(RUNNER) run system --keep-going $(ARGS)

test-e2e:
	$(RUNNER) run e2e --keep-going $(ARGS)

test-ui:
	$(RUNNER) run ui --keep-going $(ARGS)

test-all:
	$(RUNNER) run all --keep-going $(ARGS)

test-list:
	$(RUNNER) list $(ARGS)

test-doctor:
	$(RUNNER) doctor $(ARGS)

test-dry-run:
	$(RUNNER) run all --dry-run $(ARGS)

test-report:
	$(RUNNER) report $(ARGS)

test-collect:
	$(RUNNER) collect $(ARGS)

test-trace:
	$(RUNNER) trace $(ARGS)

test-db-create:
	$(RUNNER) db create

test-db-drop:
	$(RUNNER) db drop

test-db-reset:
	$(RUNNER) db reset

# The env targets print the commands; add ARGS=--execute to run them.
test-env-up:
	$(RUNNER) env up $(ARGS)

test-env-down:
	$(RUNNER) env down $(ARGS)

test-env-up-e2e:
	$(RUNNER) env up --e2e $(ARGS)

test-env-down-e2e:
	$(RUNNER) env down --e2e $(ARGS)
