-- DROP SCHEMA dev;

CREATE SCHEMA dev AUTHORIZATION postgres;

-- DROP SEQUENCE dev.categories_id_seq;

CREATE SEQUENCE dev.categories_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;

-- Permissions

ALTER SEQUENCE dev.categories_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE dev.categories_id_seq TO postgres;
GRANT ALL ON SEQUENCE dev.categories_id_seq TO dev_todo_grocery;

-- DROP SEQUENCE dev.task_assignment_id_seq;

CREATE SEQUENCE dev.task_assignment_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;

-- Permissions

ALTER SEQUENCE dev.task_assignment_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE dev.task_assignment_id_seq TO postgres;
GRANT ALL ON SEQUENCE dev.task_assignment_id_seq TO dev_todo_grocery;

-- DROP SEQUENCE dev.tasks_id_seq;

CREATE SEQUENCE dev.tasks_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;

-- Permissions

ALTER SEQUENCE dev.tasks_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE dev.tasks_id_seq TO postgres;
GRANT ALL ON SEQUENCE dev.tasks_id_seq TO dev_todo_grocery;

-- DROP SEQUENCE dev.temporary_data_id_seq;

CREATE SEQUENCE dev.temporary_data_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;

-- Permissions

ALTER SEQUENCE dev.temporary_data_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE dev.temporary_data_id_seq TO postgres;
GRANT ALL ON SEQUENCE dev.temporary_data_id_seq TO dev_todo_grocery;

-- DROP SEQUENCE dev.updates_log_id_seq;

CREATE SEQUENCE dev.updates_log_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;

-- Permissions

ALTER SEQUENCE dev.updates_log_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE dev.updates_log_id_seq TO postgres;
GRANT ALL ON SEQUENCE dev.updates_log_id_seq TO dev_todo_grocery;
-- dev.categories definition

-- Drop table

-- DROP TABLE dev.categories;

CREATE TABLE dev.categories (
	id serial4 NOT NULL, -- Unique category ID
	"name" varchar(255) NOT NULL, -- Category name (unique)
	created_at timestamp DEFAULT now() NULL,
	updated_at timestamp DEFAULT now() NULL,
	"position" int4 DEFAULT 0 NULL,
	CONSTRAINT categories_name_key UNIQUE (name),
	CONSTRAINT categories_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE dev.categories IS 'Stores categories of tasks';

-- Column comments

COMMENT ON COLUMN dev.categories.id IS 'Unique category ID';
COMMENT ON COLUMN dev.categories."name" IS 'Category name (unique)';

-- Permissions

ALTER TABLE dev.categories OWNER TO postgres;
GRANT ALL ON TABLE dev.categories TO postgres;
GRANT INSERT, DELETE, UPDATE, SELECT ON TABLE dev.categories TO dev_todo_grocery;


-- dev.tasks definition

-- Drop table

-- DROP TABLE dev.tasks;

CREATE TABLE dev.tasks (
	id serial4 NOT NULL, -- Unique task ID
	"name" varchar(255) NOT NULL, -- Task name
	description text NULL, -- Task description
	created_at timestamp DEFAULT now() NULL,
	updated_at timestamp DEFAULT now() NULL,
	is_done bool DEFAULT false NULL,
	CONSTRAINT tasks_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE dev.tasks IS 'Stores tasks';

-- Column comments

COMMENT ON COLUMN dev.tasks.id IS 'Unique task ID';
COMMENT ON COLUMN dev.tasks."name" IS 'Task name';
COMMENT ON COLUMN dev.tasks.description IS 'Task description';

-- Permissions

ALTER TABLE dev.tasks OWNER TO postgres;
GRANT ALL ON TABLE dev.tasks TO postgres;
GRANT INSERT, DELETE, UPDATE, SELECT ON TABLE dev.tasks TO dev_todo_grocery;


-- dev.temporary_data definition

-- Drop table

-- DROP TABLE dev.temporary_data;

CREATE TABLE dev.temporary_data (
	id serial4 NOT NULL,
	field01 varchar(255) DEFAULT NULL::character varying NULL,
	field02 varchar(255) DEFAULT NULL::character varying NULL,
	field03 varchar(255) DEFAULT NULL::character varying NULL,
	field04 varchar(255) DEFAULT NULL::character varying NULL,
	field05 varchar(255) DEFAULT NULL::character varying NULL,
	field06 varchar(255) DEFAULT NULL::character varying NULL,
	field07 varchar(255) DEFAULT NULL::character varying NULL,
	field08 varchar(255) DEFAULT NULL::character varying NULL,
	field09 varchar(255) DEFAULT NULL::character varying NULL,
	field10 varchar(255) DEFAULT NULL::character varying NULL,
	CONSTRAINT temporary_data_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE dev.temporary_data IS 'Stores temporary data';

-- Permissions

ALTER TABLE dev.temporary_data OWNER TO postgres;
GRANT ALL ON TABLE dev.temporary_data TO postgres;
GRANT INSERT, DELETE, UPDATE, SELECT ON TABLE dev.temporary_data TO dev_todo_grocery;


-- dev.updates_log definition

-- Drop table

-- DROP TABLE dev.updates_log;

CREATE TABLE dev.updates_log (
	id serial4 NOT NULL,
	"action" varchar(255) NOT NULL,
	event_data jsonb NOT NULL,
	created_at timestamp DEFAULT CURRENT_TIMESTAMP NULL,
	CONSTRAINT updates_log_pkey PRIMARY KEY (id)
);
CREATE INDEX idx_updates_log_time ON dev.updates_log USING brin (created_at, id);

-- Permissions

ALTER TABLE dev.updates_log OWNER TO postgres;
GRANT ALL ON TABLE dev.updates_log TO postgres;
GRANT INSERT, DELETE, UPDATE, SELECT ON TABLE dev.updates_log TO dev_todo_grocery;


-- dev.task_assignment definition

-- Drop table

-- DROP TABLE dev.task_assignment;

CREATE TABLE dev.task_assignment (
	id serial4 NOT NULL, -- Unique task assignment ID
	task_id int4 NOT NULL, -- Reference to a task
	category_id int4 NOT NULL, -- Reference to a category
	assigned_at timestamp DEFAULT now() NULL, -- Timestamp of assignment
	CONSTRAINT task_assignment_pkey PRIMARY KEY (id),
	CONSTRAINT fk_category FOREIGN KEY (category_id) REFERENCES dev.categories(id) ON DELETE CASCADE,
	CONSTRAINT fk_task FOREIGN KEY (task_id) REFERENCES dev.tasks(id) ON DELETE CASCADE
);
COMMENT ON TABLE dev.task_assignment IS 'Links tasks to categories';

-- Column comments

COMMENT ON COLUMN dev.task_assignment.id IS 'Unique task assignment ID';
COMMENT ON COLUMN dev.task_assignment.task_id IS 'Reference to a task';
COMMENT ON COLUMN dev.task_assignment.category_id IS 'Reference to a category';
COMMENT ON COLUMN dev.task_assignment.assigned_at IS 'Timestamp of assignment';

-- Permissions

ALTER TABLE dev.task_assignment OWNER TO postgres;
GRANT ALL ON TABLE dev.task_assignment TO postgres;
GRANT INSERT, DELETE, UPDATE, SELECT ON TABLE dev.task_assignment TO dev_todo_grocery;



-- DROP FUNCTION dev.handle_category();

CREATE OR REPLACE FUNCTION dev.handle_category()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    temp_row dev.temporary_data%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
		INSERT INTO dev.updates_log (action, event_data)
    	VALUES ('ADD_CATEGORY', json_build_object(
	        'table', 'categories',
	        'action', 'ADD_CATEGORY',
	        'category_id', NEW.id,
	        'name', NEW.name
    	));
        PERFORM pg_notify('data_changes', json_build_object(
            'table', 'categories',
            'action', 'ADD_CATEGORY',
            'category_id', NEW.id,
            'name', NEW.name
        )::text);
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.name <> OLD.name THEN
			INSERT INTO dev.updates_log (action, event_data)
	    	VALUES ('RENAME_CATEGORY', json_build_object(
                'table', 'categories',
                'action', 'RENAME_CATEGORY',
                'category_id', NEW.id,
                'new_name', NEW.name
	    	));
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'categories',
                'action', 'RENAME_CATEGORY',
                'category_id', NEW.id,
                'new_name', NEW.name
            )::text);
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT * INTO temp_row FROM dev.temporary_data
        WHERE field01 = 'categories' AND field02 = 'DELETE_CATEGORY'
        ORDER BY id DESC LIMIT 1;

		IF FOUND THEN
			INSERT INTO dev.updates_log (action, event_data)
	    	VALUES ('DELETE_CATEGORY', json_build_object(
	            'table', 'categories',
	            'action', 'DELETE_CATEGORY',
	            'category_id', OLD.id
	    	));

	        PERFORM pg_notify('data_changes', json_build_object(
	            'table', 'categories',
	            'action', 'DELETE_CATEGORY',
	            'category_id', OLD.id
	        )::text);
		END IF;

		DELETE FROM dev.temporary_data WHERE id = temp_row.id;
    END IF;

    RETURN NEW;
END;
$function$
;

-- Permissions

ALTER FUNCTION dev.handle_category() OWNER TO postgres;
GRANT ALL ON FUNCTION dev.handle_category() TO postgres;

-- DROP FUNCTION dev.handle_task();

CREATE OR REPLACE FUNCTION dev.handle_task()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.is_done <> OLD.is_done THEN
			INSERT INTO dev.updates_log (action, event_data)
	    	VALUES ('TOGGLE_TASK', json_build_object(
                'table', 'tasks',
                'action', 'TOGGLE_TASK',
                'task_id', NEW.id,
                'category_id', (SELECT category_id FROM dev.task_assignment WHERE task_id = NEW.id)
	    	));
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'tasks',
                'action', 'TOGGLE_TASK',
                'task_id', NEW.id,
                'category_id', (SELECT category_id FROM dev.task_assignment WHERE task_id = NEW.id)
            )::text);
        ELSIF NEW.name <> OLD.name OR NEW.description <> OLD.description THEN
			INSERT INTO dev.updates_log (action, event_data)
	    	VALUES ('UPDATE_TASK', json_build_object(
                'table', 'tasks',
                'action', 'UPDATE_TASK',
                'task_id', NEW.id,
                'category_id', (SELECT category_id FROM dev.task_assignment WHERE task_id = NEW.id),
                'new_name', NEW.name,
                'new_description', NEW.description
	    	));
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'tasks',
                'action', 'UPDATE_TASK',
                'task_id', NEW.id,
                'category_id', (SELECT category_id FROM dev.task_assignment WHERE task_id = NEW.id),
                'new_name', NEW.name,
                'new_description', NEW.description
            )::text);
        END IF;
    END IF;
    RETURN NEW;
END;
$function$
;

-- Permissions

ALTER FUNCTION dev.handle_task() OWNER TO postgres;
GRANT ALL ON FUNCTION dev.handle_task() TO postgres;

-- DROP FUNCTION dev.handle_task_assignment();

CREATE OR REPLACE FUNCTION dev.handle_task_assignment()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    task_name TEXT;
    temp_row dev.temporary_data%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT name INTO task_name FROM dev.tasks WHERE id = NEW.task_id;
		INSERT INTO dev.updates_log (action, event_data)
	    	VALUES ('ADD_TASK', json_build_object(
            'table', 'task_assignment',
            'action', 'ADD_TASK',
            'task_id', NEW.task_id,
            'category_id', NEW.category_id,
            'task_name', task_name
	    ));
        PERFORM pg_notify('data_changes', json_build_object(
            'table', 'task_assignment',
            'action', 'ADD_TASK',
            'task_id', NEW.task_id,
            'category_id', NEW.category_id,
            'task_name', task_name
        )::text);
        
    ELSIF TG_OP = 'DELETE' THEN
        SELECT * INTO temp_row FROM dev.temporary_data
        WHERE field01 = 'tasks' AND field02 = 'DELETE_TASK'
        ORDER BY id DESC LIMIT 1;
        
        IF FOUND THEN
            SELECT name INTO task_name FROM dev.tasks WHERE id = OLD.task_id;

			INSERT INTO dev.updates_log (action, event_data)
	    	VALUES ('DELETE_TASK', json_build_object(
                'table', 'task_assignment',
                'action', 'DELETE_TASK',
                'task_id', OLD.task_id,
                'category_id', OLD.category_id,
                'task_name', task_name
	    	));
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'task_assignment',
                'action', 'DELETE_TASK',
                'task_id', OLD.task_id,
                'category_id', OLD.category_id,
                'task_name', task_name
            )::text);
        END IF;

		DELETE FROM dev.temporary_data WHERE id = temp_row.id;
    END IF;
    RETURN NEW;
END;
$function$
;

-- Permissions

ALTER FUNCTION dev.handle_task_assignment() OWNER TO postgres;
GRANT ALL ON FUNCTION dev.handle_task_assignment() TO postgres;

-- DROP FUNCTION dev.handle_task_position();

CREATE OR REPLACE FUNCTION dev.handle_task_position()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.category_id <> OLD.category_id THEN
		INSERT INTO dev.updates_log (action, event_data)
	    VALUES ('MOVE_TASK', json_build_object(
            'table', 'task_assignment',
            'action', 'MOVE_TASK',
            'task_id', NEW.task_id,
            'old_category_id', OLD.category_id,
            'new_category_id', NEW.category_id,
            'task_name', (SELECT name FROM dev.tasks WHERE id = NEW.task_id)
	    ));

        PERFORM pg_notify('data_changes', json_build_object(
            'table', 'task_assignment',
            'action', 'MOVE_TASK',
            'task_id', NEW.task_id,
            'old_category_id', OLD.category_id,
            'new_category_id', NEW.category_id,
            'task_name', (SELECT name FROM dev.tasks WHERE id = NEW.task_id)
        )::text);
    END IF;
    RETURN NEW;
END;
$function$
;

-- Permissions

ALTER FUNCTION dev.handle_task_position() OWNER TO postgres;
GRANT ALL ON FUNCTION dev.handle_task_position() TO postgres;

-- DROP FUNCTION dev.handle_temp_move_cat_data();

CREATE OR REPLACE FUNCTION dev.handle_temp_move_cat_data()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    temp_row dev.temporary_data%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO temp_row FROM dev.temporary_data
        WHERE field01 = 'categories' AND field02 = 'MOVE_CATEGORY'
        ORDER BY id DESC LIMIT 1;

        IF FOUND THEN
			INSERT INTO dev.updates_log (action, event_data)
		    	VALUES ('MOVE_CATEGORY', json_build_object(
                'table', temp_row.field01,
                'action', temp_row.field02,
                'category_id', temp_row.field03,
                'direction', temp_row.field04
		    ));
            PERFORM pg_notify('data_changes', json_build_object(
                'table', temp_row.field01,
                'action', temp_row.field02,
                'category_id', temp_row.field03,
                'direction', temp_row.field04
            )::text);
        END IF;
		DELETE FROM dev.temporary_data WHERE id = temp_row.id;
    END IF;
    RETURN NEW;
END;
$function$
;

-- Permissions

ALTER FUNCTION dev.handle_temp_move_cat_data() OWNER TO postgres;
GRANT ALL ON FUNCTION dev.handle_temp_move_cat_data() TO postgres;


-- Permissions

GRANT ALL ON SCHEMA dev TO postgres;
GRANT USAGE ON SCHEMA dev TO dev_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dev GRANT INSERT, DELETE, UPDATE, SELECT ON TABLES TO dev_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dev GRANT USAGE, UPDATE, SELECT ON SEQUENCES TO dev_todo_grocery;


--  Table Triggers [categories]

create trigger category_trigger after
insert
    or
delete
    or
update
    on
    dev.categories for each row execute function dev.handle_category();

-- Table Triggers [tasks]

create trigger task_trigger after
update
    on
    dev.tasks for each row execute function dev.handle_task();

-- Table Triggers [temporary_data]

create trigger handle_temp_move_cat_data_trigger after
insert
    on
    dev.temporary_data for each row execute function dev.handle_temp_move_cat_data();

-- Table Triggers [task_assignment]

create trigger task_assignment_insert_trigger after
insert
    on
    dev.task_assignment for each row execute function dev.handle_task_assignment();
create trigger task_assignment_delete_trigger after
delete
    on
    dev.task_assignment for each row execute function dev.handle_task_assignment();
create trigger task_position_trigger after
update
    of category_id on
    dev.task_assignment for each row execute function dev.handle_task_position();