-- DROP SCHEMA prod;

CREATE SCHEMA prod AUTHORIZATION postgres;

-- DROP SEQUENCE prod.categories_id_seq;

CREATE SEQUENCE prod.categories_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;

-- Permissions

ALTER SEQUENCE prod.categories_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE prod.categories_id_seq TO postgres;
GRANT ALL ON SEQUENCE prod.categories_id_seq TO prod_todo_grocery;

-- DROP SEQUENCE prod.task_assignment_id_seq;

CREATE SEQUENCE prod.task_assignment_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;

-- Permissions

ALTER SEQUENCE prod.task_assignment_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE prod.task_assignment_id_seq TO postgres;
GRANT ALL ON SEQUENCE prod.task_assignment_id_seq TO prod_todo_grocery;

-- DROP SEQUENCE prod.tasks_id_seq;

CREATE SEQUENCE prod.tasks_id_seq
	INCREMENT BY 1
	MINVALUE 1
	MAXVALUE 2147483647
	START 1
	CACHE 1
	NO CYCLE;

-- Permissions

ALTER SEQUENCE prod.tasks_id_seq OWNER TO postgres;
GRANT ALL ON SEQUENCE prod.tasks_id_seq TO postgres;
GRANT ALL ON SEQUENCE prod.tasks_id_seq TO prod_todo_grocery;
-- prod.categories definition

-- Drop table

-- DROP TABLE prod.categories;

CREATE TABLE prod.categories (
	id serial4 NOT NULL, -- Unique category ID
	"name" varchar(255) NOT NULL, -- Category name (unique)
	created_at timestamp DEFAULT now() NULL,
	updated_at timestamp DEFAULT now() NULL,
	"position" int4 DEFAULT 0 NULL,
	CONSTRAINT categories_name_key UNIQUE (name),
	CONSTRAINT categories_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE prod.categories IS 'Stores categories of tasks';

-- Column comments

COMMENT ON COLUMN prod.categories.id IS 'Unique category ID';
COMMENT ON COLUMN prod.categories."name" IS 'Category name (unique)';

-- Table Triggers

create trigger category_trigger after
insert
    or
delete
    or
update
    on
    prod.categories for each row execute function prod.handle_category();

-- Permissions

ALTER TABLE prod.categories OWNER TO postgres;
GRANT ALL ON TABLE prod.categories TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.categories TO prod_todo_grocery;


-- prod.tasks definition

-- Drop table

-- DROP TABLE prod.tasks;

CREATE TABLE prod.tasks (
	id serial4 NOT NULL, -- Unique task ID
	"name" varchar(255) NOT NULL, -- Task name
	description text NULL, -- Task description
	created_at timestamp DEFAULT now() NULL,
	updated_at timestamp DEFAULT now() NULL,
	is_done bool DEFAULT false NULL,
	CONSTRAINT tasks_pkey PRIMARY KEY (id)
);
COMMENT ON TABLE prod.tasks IS 'Stores tasks';

-- Column comments

COMMENT ON COLUMN prod.tasks.id IS 'Unique task ID';
COMMENT ON COLUMN prod.tasks."name" IS 'Task name';
COMMENT ON COLUMN prod.tasks.description IS 'Task description';

-- Table Triggers

create trigger task_trigger after
update
    on
    prod.tasks for each row execute function prod.handle_task();

-- Permissions

ALTER TABLE prod.tasks OWNER TO postgres;
GRANT ALL ON TABLE prod.tasks TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.tasks TO prod_todo_grocery;


-- prod.task_assignment definition

-- Drop table

-- DROP TABLE prod.task_assignment;

CREATE TABLE prod.task_assignment (
	id serial4 NOT NULL, -- Unique task assignment ID
	task_id int4 NOT NULL, -- Reference to a task
	category_id int4 NOT NULL, -- Reference to a category
	assigned_at timestamp DEFAULT now() NULL, -- Timestamp of assignment
	CONSTRAINT task_assignment_pkey PRIMARY KEY (id),
	CONSTRAINT fk_category FOREIGN KEY (category_id) REFERENCES prod.categories(id) ON DELETE CASCADE,
	CONSTRAINT fk_task FOREIGN KEY (task_id) REFERENCES prod.tasks(id) ON DELETE CASCADE
);
COMMENT ON TABLE prod.task_assignment IS 'Links tasks to categories';

-- Column comments

COMMENT ON COLUMN prod.task_assignment.id IS 'Unique task assignment ID';
COMMENT ON COLUMN prod.task_assignment.task_id IS 'Reference to a task';
COMMENT ON COLUMN prod.task_assignment.category_id IS 'Reference to a category';
COMMENT ON COLUMN prod.task_assignment.assigned_at IS 'Timestamp of assignment';

-- Table Triggers

create trigger task_assignment_insert_trigger after
insert
    on
    prod.task_assignment for each row execute function prod.handle_task_assignment();
create trigger task_assignment_delete_trigger after
delete
    on
    prod.task_assignment for each row execute function prod.handle_task_assignment();
create trigger task_position_trigger after
update
    of category_id on
    prod.task_assignment for each row execute function prod.handle_task_position();

-- Permissions

ALTER TABLE prod.task_assignment OWNER TO postgres;
GRANT ALL ON TABLE prod.task_assignment TO postgres;
GRANT DELETE, SELECT, INSERT, UPDATE ON TABLE prod.task_assignment TO prod_todo_grocery;



-- DROP FUNCTION prod.handle_category();

CREATE OR REPLACE FUNCTION prod.handle_category()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    direction text;
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
ELSIF NEW.position <> OLD.position THEN
            direction := CASE 
                WHEN NEW.position > OLD.position THEN 'down'
                ELSE 'up'
            END;
            
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'category',
                'action', 'MOVE_CATEGORY',
                'category_id', NEW.id,
                'direction', direction
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

ALTER FUNCTION prod.handle_category() OWNER TO postgres;
GRANT ALL ON FUNCTION prod.handle_category() TO postgres;

-- DROP FUNCTION prod.handle_task();

CREATE OR REPLACE FUNCTION prod.handle_task()
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
                'category_id', (SELECT category_id FROM prod.task_assignment WHERE task_id = NEW.id)
            )::text);
        ELSIF NEW.name <> OLD.name OR NEW.description <> OLD.description THEN
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'task',
                'action', 'UPDATE_TASK',
                'task_id', NEW.id,
                'category_id', (SELECT category_id FROM prod.task_assignment WHERE task_id = NEW.id),
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

ALTER FUNCTION prod.handle_task() OWNER TO postgres;
GRANT ALL ON FUNCTION prod.handle_task() TO postgres;

-- DROP FUNCTION prod.handle_task_assignment();

CREATE OR REPLACE FUNCTION prod.handle_task_assignment()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    task_name TEXT;
	category_exists BOOLEAN;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT name INTO task_name FROM prod.tasks WHERE id = NEW.task_id;
        PERFORM pg_notify('data_changes', json_build_object(
            'table', 'task_assignment',
            'action', 'ADD_TASK',
            'task_id', NEW.task_id,
            'category_id', NEW.category_id,
            'task_name', task_name
        )::text);
        
    ELSIF TG_OP = 'DELETE' THEN
SELECT EXISTS(SELECT 1 FROM prod.categories WHERE id = OLD.category_id) INTO category_exists;
        
        IF category_exists THEN
            SELECT name INTO task_name FROM prod.tasks WHERE id = OLD.task_id;
            PERFORM pg_notify('data_changes', json_build_object(
                'table', 'task_assignment',
                'action', 'DELETE_TASK',
                'task_id', OLD.task_id,
                'category_id', OLD.category_id,
                'task_name', task_name
            )::text);
        END IF;
    END IF;
    RETURN NEW;
END;
$function$
;

-- Permissions

ALTER FUNCTION prod.handle_task_assignment() OWNER TO postgres;
GRANT ALL ON FUNCTION prod.handle_task_assignment() TO postgres;

-- DROP FUNCTION prod.handle_task_position();

CREATE OR REPLACE FUNCTION prod.handle_task_position()
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
            'task_name', (SELECT name FROM prod.tasks WHERE id = NEW.task_id)
        )::text);
    END IF;
    RETURN NEW;
END;
$function$
;

-- Permissions

ALTER FUNCTION prod.handle_task_position() OWNER TO postgres;
GRANT ALL ON FUNCTION prod.handle_task_position() TO postgres;


-- Permissions

GRANT ALL ON SCHEMA prod TO postgres;
GRANT USAGE ON SCHEMA prod TO prod_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA prod GRANT DELETE, SELECT, INSERT, UPDATE ON TABLES TO prod_todo_grocery;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA prod GRANT SELECT, USAGE, UPDATE ON SEQUENCES TO prod_todo_grocery;