# Pull Request Guidelines

To ensure a smooth and efficient process for raising and merging pull requests (PRs), please adhere to the following guidelines:

## 1. Keep Pull Requests Small

- **Limit the Size**: Aim for a maximum of **200-400 lines of code (LOC)** per PR, with an ideal target of **less than 50 LOC**. This facilitates easier reviews and reduces the likelihood of introducing bugs.
- **Focus on Specific Changes**: Each PR should address a single purpose or feature to maintain clarity and focus.

## 2. Write Clear Commit Messages

- **Follow Conventional Commits**: Use the [Conventional Commits](https://www.conventionalcommits.org/) format for commit messages:
  - Format: `type(scope): description`
  - Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`
  - Example: `feat(auth): add OAuth2 authentication flow`
  - This standardized format helps with automated versioning and changelog generation

## 3. Review Your Own Code First

- **Self-Review**: Before submitting a PR, review your own code to catch any errors or typos. This step ensures that the initial submission is as polished as possible.

## 4. Include Tests

- **Testing Requirements**: All PRs must include relevant unit tests to verify the functionality of the new code. This practice helps maintain code integrity and reduces future debugging efforts.
- **Integration and Scale Tests**: If your PR targets significant backend feature changes, ensure you make updates to integration tests and scale tests using Playwright. This will help verify backward compatibility and ensure that your changes are tested against any future modifications.

## 5. PR Review for Structural Changes

- **Update Class Diagrams**: If your PR includes changes to the class structure, please ensure you update the class diagram file in the repository. Get this updated diagram reviewed first before making any structural changes.

## 6. Use Draft Pull Requests

- **Early Feedback**: Utilize the draft pull request feature to solicit early feedback from teammates before marking it as ready for review. This can help identify potential issues early in the process.

## 7. Implement Branch Protection Rules

- **Approval Requirements**: At least two team members need to approve a PR before it can be merged. This ensures that all changes are reviewed by a peer, enhancing code quality.

## 8. Communicate Effectively

- **Notify Reviewers**: After submitting a PR, proactively notify team members to encourage timely reviews. Regular communication helps prevent delays in the review process.

## 9. Code Review Norms

To cut through ambiguity in code review comments, please adopt the following notation:

- **BLOCKER**: Must be addressed before this PR can be merged.

  - Example: “BLOCKER: this API is not available on mobile browsers and will cause the phone to implode”

- **FAST-FOLLOW**: Should be addressed, but optionally via a follow-up PR.

  - Example: “FAST-FOLLOW: We really shouldn’t have this business logic in a controller action - please pull it out into a domain class”

- **NIT**: Feedback that is optional; an opportunity for discussion on team norms.
  - Example: “NIT: I would probably have pulled this out into a little helper function”

By adhering to these guidelines, we can improve our pull request processes, leading to better collaboration, higher code quality, and increased efficiency in our software development efforts.
