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

-- Table Triggers

create trigger category_trigger after
insert
    or
delete
    or
update
    on
    dev.categories for each row execute function dev.handle_category();

-- Permissions

ALTER TABLE dev.categories OWNER TO postgres;
GRANT ALL ON TABLE dev.categories TO postgres;
GRANT UPDATE, SELECT, INSERT, DELETE ON TABLE dev.categories TO dev_todo_grocery;


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

-- Table Triggers

create trigger task_trigger after
update
    on
    dev.tasks for each row execute function dev.handle_task();

-- Permissions

ALTER TABLE dev.tasks OWNER TO postgres;
GRANT ALL ON TABLE dev.tasks TO postgres;
GRANT UPDATE, SELECT, INSERT, DELETE ON TABLE dev.tasks TO dev_todo_grocery;


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

-- Table Triggers

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

-- Permissions

ALTER TABLE dev.task_assignment OWNER TO postgres;
GRANT ALL ON TABLE dev.task_assignment TO postgres;
GRANT UPDATE, SELECT, INSERT, DELETE ON TABLE dev.task_assignment TO dev_todo_grocery;



-- DROP FUNCTION dev.handle_category();

CREATE OR REPLACE FUNCTION dev.handle_category()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM pg_notify('data_changes', json_build_object(
            'table', 'category',
            'action', 'ADD_CATEGORY',
            'category_id', NEW.id,
            'name', NEW.name
        )::text);
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.name <> OLD.name THEN
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'category',
                'action', 'RENAME_CATEGORY',
                'category_id', NEW.id,
                'new_name', NEW.name
            )::text);
        ELSE
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'category',
                'action', 'MOVE_CATEGORY',
                'category_id', NEW.id
            )::text);
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM pg_notify('data_changes', json_build_object(
            'table', 'category',
            'action', 'DELETE_CATEGORY',
            'category_id', OLD.id
        )::text);
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
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'task',
                'action', 'TOGGLE_TASK',
                'task_id', NEW.id,
                'category_id', (SELECT category_id FROM dev.task_assignment WHERE task_id = NEW.id)
            )::text);
        ELSIF NEW.name <> OLD.name OR NEW.description <> OLD.description THEN
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'task',
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
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT name INTO task_name FROM dev.tasks WHERE id = NEW.task_id;
        PERFORM pg_notify('data_changes', json_build_object(
            'table', 'task_assignment',
            'action', 'ADD_TASK',
            'task_id', NEW.task_id,
            'category_id', NEW.category_id,
            'task_name', task_name
        )::text);
        
    ELSIF TG_OP = 'DELETE' THEN
        SELECT name INTO task_name FROM dev.tasks WHERE id = OLD.task_id;
        PERFORM pg_notify('data_changes', json_build_object(
            'table', 'task_assignment',
            'action', 'DELETE_TASK',
            'task_id', OLD.task_id,
            'category_id', OLD.category_id,
            'task_name', task_name
        )::text);
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


-- Permissions

GRANT ALL ON SCHEMA dev TO postgres;
GRANT USAGE ON SCHEMA dev TO dev_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dev GRANT UPDATE, SELECT, INSERT, DELETE ON TABLES TO dev_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dev GRANT UPDATE, SELECT, USAGE ON SEQUENCES TO dev_todo_grocery;