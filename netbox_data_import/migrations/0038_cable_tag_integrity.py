from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dcim", "__first__"),
        ("extras", "__first__"),
        ("netbox_data_import", "0037_cablesegmentoverride"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                LOCK TABLE django_content_type IN SHARE ROW EXCLUSIVE MODE;
                LOCK TABLE dcim_cable IN SHARE ROW EXCLUSIVE MODE;
                LOCK TABLE extras_taggeditem IN ACCESS EXCLUSIVE MODE;

                ALTER TABLE extras_taggeditem ADD COLUMN ndi_cable_id bigint;

                CREATE FUNCTION ndi_set_cable_tag_id() RETURNS trigger
                LANGUAGE plpgsql VOLATILE AS $$
                DECLARE
                    target_app text;
                    target_model text;
                BEGIN
                    SELECT app_label, model INTO target_app, target_model
                    FROM django_content_type
                    WHERE id = NEW.content_type_id
                    FOR SHARE;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'TaggedItem content type % does not exist', NEW.content_type_id
                            USING ERRCODE = '23503';
                    END IF;

                    IF target_app = 'dcim' AND target_model = 'cable' THEN
                        NEW.ndi_cable_id := NEW.object_id;
                    ELSE
                        NEW.ndi_cable_id := NULL;
                    END IF;
                    RETURN NEW;
                END;
                $$;

                CREATE TRIGGER ndi_derive_cable_tag_id
                BEFORE INSERT OR UPDATE ON extras_taggeditem
                FOR EACH ROW EXECUTE FUNCTION ndi_set_cable_tag_id();

                UPDATE extras_taggeditem AS item
                SET ndi_cable_id = item.object_id
                FROM django_content_type AS kind
                WHERE kind.id = item.content_type_id
                  AND kind.app_label = 'dcim'
                  AND kind.model = 'cable';

                CREATE INDEX ndi_taggeditem_cable_id
                ON extras_taggeditem (ndi_cable_id)
                WHERE ndi_cable_id IS NOT NULL;

                ALTER TABLE extras_taggeditem
                ADD CONSTRAINT ndi_taggeditem_cable_fk
                FOREIGN KEY (ndi_cable_id) REFERENCES dcim_cable (id)
                ON DELETE NO ACTION ON UPDATE NO ACTION
                DEFERRABLE INITIALLY DEFERRED;

                CREATE FUNCTION ndi_guard_cable_content_type() RETURNS trigger
                LANGUAGE plpgsql VOLATILE AS $$
                BEGIN
                    IF (OLD.app_label, OLD.model) IS NOT DISTINCT FROM
                       (NEW.app_label, NEW.model) THEN
                        RETURN NEW;
                    END IF;
                    IF (OLD.app_label = 'dcim' AND OLD.model = 'cable') OR
                       (NEW.app_label = 'dcim' AND NEW.model = 'cable') THEN
                        IF current_setting('transaction_isolation') <> 'read committed' THEN
                            RAISE EXCEPTION 'Cable content type identity requires READ COMMITTED'
                                USING ERRCODE = '25001';
                        END IF;
                        IF EXISTS (
                            SELECT 1 FROM extras_taggeditem
                            WHERE content_type_id = OLD.id
                        ) THEN
                            RAISE EXCEPTION 'Cable content type identity has tagged items'
                                USING ERRCODE = '23514';
                        END IF;
                    END IF;
                    RETURN NEW;
                END;
                $$;

                CREATE TRIGGER ndi_guard_cable_content_type_identity
                BEFORE UPDATE OF app_label, model ON django_content_type
                FOR EACH ROW EXECUTE FUNCTION ndi_guard_cable_content_type();
            """,
            reverse_sql="""
                DROP TRIGGER ndi_guard_cable_content_type_identity ON django_content_type;
                DROP FUNCTION ndi_guard_cable_content_type();
                ALTER TABLE extras_taggeditem DROP CONSTRAINT ndi_taggeditem_cable_fk;
                DROP TRIGGER ndi_derive_cable_tag_id ON extras_taggeditem;
                DROP FUNCTION ndi_set_cable_tag_id();
                DROP INDEX ndi_taggeditem_cable_id;
                ALTER TABLE extras_taggeditem DROP COLUMN ndi_cable_id;
            """,
        ),
    ]
