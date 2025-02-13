-- This script screates function to notify about data change
-- PROD schema
SET search_path TO prod;

CREATE OR REPLACE FUNCTION notify_data_change()
RETURNS TRIGGER AS $$
DECLARE
    payload JSON; -- Deklaracja zmiennej
BEGIN
    IF TG_OP = 'DELETE' THEN
        payload := json_build_object(
            'operation', TG_OP,
            'table', TG_TABLE_NAME,
            'id', OLD.id
        );
    ELSE
        payload := json_build_object(
        'task_id', NEW.id,
        'category_id', (
        SELECT category_id
        FROM prod.task_assignment
        WHERE task_id = NEW.id
        )
    );
    END IF;

    PERFORM pg_notify('data_changes', payload::text);
    RETURN NEW;
END;
$$
 LANGUAGE plpgsql;


-----------------------------------------------
-- DEV schema
SET search_path TO dev;

CREATE OR REPLACE FUNCTION notify_data_change()
RETURNS TRIGGER AS $$
DECLARE
    payload JSON; -- Deklaracja zmiennej
BEGIN
    IF TG_OP = 'DELETE' THEN
        payload := json_build_object(
            'operation', TG_OP,
            'table', TG_TABLE_NAME,
            'id', OLD.id
        );
    ELSE
        payload := json_build_object(
        'task_id', NEW.id,
        'category_id', (
        SELECT category_id
        FROM dev.task_assignment
        WHERE task_id = NEW.id
        )
    );
    END IF;

    PERFORM pg_notify('data_changes', payload::text);
    RETURN NEW;
END;
$$
 LANGUAGE plpgsql;




