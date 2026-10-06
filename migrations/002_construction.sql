-- Domain model. Names follow the blueprint's schema; additions are marked (+).

CREATE TABLE projects (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants UNIQUE, name text NOT NULL, start_date date NOT NULL, planned_end date NOT NULL, budget numeric NOT NULL, model_seed int NOT NULL, now_day int NOT NULL);   -- (+) seed and clock
SELECT enable_tenant_rls('projects');

CREATE TABLE activities (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, project_id uuid NOT NULL REFERENCES projects ON DELETE CASCADE, external_id text NOT NULL, name text NOT NULL, start_planned date NOT NULL, end_planned date NOT NULL,
  percent_complete numeric NOT NULL DEFAULT 0, cost_code text NOT NULL, ordinal int NOT NULL, trade text NOT NULL, duration_days int NOT NULL, cost numeric NOT NULL, floor int, outdoor boolean NOT NULL, procurement_item text, status text NOT NULL,   -- (+)
  actual_start date, actual_end date, UNIQUE (project_id, external_id));
SELECT enable_tenant_rls('activities');

CREATE TABLE dependencies (predecessor_id uuid NOT NULL REFERENCES activities ON DELETE CASCADE, successor_id uuid NOT NULL REFERENCES activities ON DELETE CASCADE, relation text NOT NULL DEFAULT 'FS', lag_days int NOT NULL DEFAULT 0, tenant_id uuid NOT NULL REFERENCES tenants,
  PRIMARY KEY (predecessor_id, successor_id));
SELECT enable_tenant_rls('dependencies');

CREATE TABLE bim_elements (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, project_id uuid NOT NULL REFERENCES projects ON DELETE CASCADE, guid text NOT NULL, element_type text NOT NULL, level text NOT NULL, geom_ref text, activity_id uuid REFERENCES activities,
  UNIQUE (project_id, guid));
SELECT enable_tenant_rls('bim_elements');

CREATE TABLE procurement_items (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, project_id uuid NOT NULL REFERENCES projects ON DELETE CASCADE, item text NOT NULL, lead_days int NOT NULL, ordered_day int NOT NULL, expected_day int NOT NULL, delivered_day int, cost numeric NOT NULL,
  feeds_trade text NOT NULL, UNIQUE (project_id, item));   -- (+)
SELECT enable_tenant_rls('procurement_items');

CREATE TABLE progress_observations (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, project_id uuid NOT NULL REFERENCES projects ON DELETE CASCADE, captured_at timestamptz NOT NULL, source text NOT NULL, media_uri text, activity_id uuid REFERENCES activities,
  progress_estimate numeric NOT NULL, confidence numeric NOT NULL, detail jsonb NOT NULL DEFAULT '{}');
SELECT enable_tenant_rls('progress_observations');

CREATE TABLE disruptions (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, project_id uuid NOT NULL REFERENCES projects ON DELETE CASCADE, kind text NOT NULL CHECK (kind IN ('procurement_delay','weather')), payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());   -- (+)
SELECT enable_tenant_rls('disruptions');

CREATE TABLE change_orders (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, project_id uuid NOT NULL REFERENCES projects ON DELETE CASCADE, title text NOT NULL, status text NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','evaluated','approved','rejected')), amount numeric NOT NULL,
  requested_days int NOT NULL, cause text NOT NULL, scope jsonb NOT NULL, impact jsonb, created_by uuid NOT NULL, decided_by uuid, created_at timestamptz NOT NULL DEFAULT now());   -- (+) scope, impact, provenance
SELECT enable_tenant_rls('change_orders');

CREATE TABLE risks (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, project_id uuid NOT NULL REFERENCES projects ON DELETE CASCADE, risk_type text NOT NULL, probability numeric NOT NULL, impact_cost numeric NOT NULL, impact_days numeric NOT NULL, evidence jsonb NOT NULL,
  subject text NOT NULL, computed_at timestamptz NOT NULL DEFAULT now(), UNIQUE (project_id, risk_type, subject));   -- (+) subject
SELECT enable_tenant_rls('risks');
