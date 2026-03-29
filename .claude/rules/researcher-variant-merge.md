# Researcher Variant Merge Protocol

When running parallel researcher variants, each produces target improvements in its own target clone. This protocol defines how to reconcile those target states back to the canonical target.

## Steps

1. PARK all researcher variants before comparing (stop process, preserve target clone state)
2. Compare researcher variant metrics: `pixi run researcher-variant compare`
3. Review parked researcher variants: `pixi run researcher-variant parked`
4. Select merge strategy based on researcher variant results:
   - One researcher variant clearly won → Winner Takes All
   - Multiple researcher variants found complementary target improvements → Cherry-Pick
   - Winning researcher variant has many accumulated target changes → Branch-and-Continue
5. Run merge: `pixi run researcher-variant merge --id <winning-rv> --strategy <strategy>`
6. Verify: run eval on canonical target, confirm target metrics match the winning researcher variant's metrics
7. If target metrics regress after merge: `pixi run researcher-variant rollback`, try different strategy
8. Discard remaining researcher variants: `pixi run researcher-variant discard --id <losing-rv>`
9. Update baseline tag: automatic after successful merge

## Choosing a Strategy

- **Winner Takes All**: Simplest. The winning researcher variant's entire target state replaces canonical. Use when one researcher variant is clearly superior.
- **Cherry-Pick**: Most surgical. Picks individual target commits from multiple researcher variants. Use when different researcher variants found complementary target improvements.
- **Branch-and-Continue**: Fastest. Moves the winning researcher variant's target clone to become canonical. Use when the winner has many target changes and cherry-picking would be tedious.

## Never merge a running researcher variant

Always park first. A running researcher variant may have uncommitted target changes, in-progress evaluations, or inconsistent state.

## Rollback

Every merge creates a backup. `pixi run researcher-variant rollback` restores the canonical target to its pre-merge state. This works for all three strategies.
