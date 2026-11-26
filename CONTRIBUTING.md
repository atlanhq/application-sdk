# Pull Request Guidelines

To ensure a smooth and efficient process for raising and merging pull requests (PRs), please adhere to the following guidelines:

## 1. Keep Pull Requests Small

- **Limit the Size**: Aim for a maximum of **200-400 lines of code (LOC)** per PR, with an ideal target of **less than 50 LOC**. This facilitates easier reviews and reduces the likelihood of introducing bugs.
- **Focus on Specific Changes**: Each PR should address a single purpose or feature to maintain clarity and focus.

## 2. Write Clear Commit Messages

- **Follow Conventional Commits**: Format commit messages and PR titles as `type(scope): description`, for example `feat(auth): add OAuth2 authentication flow`. The allowed types, scopes and rules are in [`docs/standards/commits.md`](docs/standards/commits.md); this page does not repeat them. The format drives automated versioning and changelog generation.

## 3. Review Your Own Code First

- **Self-Review**: Before submitting a PR, review your own code to catch any errors or typos. This step ensures that the initial submission is as polished as possible.

## 4. Include Tests

- **Testing Requirements**: All PRs must include relevant unit tests to verify the functionality of the new code. This practice helps maintain code integrity and reduces future debugging efforts.
- **Integration Tests**: If your PR changes how the SDK talks to Temporal, Dapr or object storage, update the integration tests under `tests/integration/` as well. How to run each tier is in [`docs/agents/dev-commands.md`](docs/agents/dev-commands.md#testing); what you need installed is in [`docs/standards/setup.md`](docs/standards/setup.md).

## 5. Use Draft Pull Requests

- **Early Feedback**: Utilize the draft pull request feature to solicit early feedback from teammates before marking it as ready for review. This can help identify potential issues early in the process.

## 6. Implement Branch Protection Rules

- **Approval Requirements**: At least two team members need to approve a PR before it can be merged. This ensures that all changes are reviewed by a peer, enhancing code quality.

## 7. Communicate Effectively

- **Notify Reviewers**: After submitting a PR, proactively notify team members to encourage timely reviews. Regular communication helps prevent delays in the review process.

## 8. Code Review Norms

To cut through ambiguity in code review comments, please adopt the following notation:

- **BLOCKER**: Must be addressed before this PR can be merged.

  - Example: “BLOCKER: this API is not available on mobile browsers and will cause the phone to implode”

- **FAST-FOLLOW**: Should be addressed, but optionally via a follow-up PR.

  - Example: “FAST-FOLLOW: We really shouldn’t have this business logic in a controller action - please pull it out into a domain class”

- **NIT**: Feedback that is optional; an opportunity for discussion on team norms.
  - Example: “NIT: I would probably have pulled this out into a little helper function”

By adhering to these guidelines, we can improve our pull request processes, leading to better collaboration, higher code quality, and increased efficiency in our software development efforts.
