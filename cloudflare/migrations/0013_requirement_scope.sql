ALTER TABLE project_requirements ADD COLUMN app_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE project_requirements ADD COLUMN cases_url TEXT NOT NULL DEFAULT '';
UPDATE project_requirements SET app_ids_json = json_array(app_id) WHERE app_id <> '';
