ALTER TABLE action_feedback
DROP CONSTRAINT IF EXISTS action_feedback_outcome_check;

ALTER TABLE action_feedback
ADD CONSTRAINT action_feedback_outcome_check CHECK (
    outcome IN (
        'done', 'not_a_task', 'not_mine', 'already_handled',
        'wrong_priority', 'snoozed', 'confirmed'
    )
);
