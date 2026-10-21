<!--SPDX-License-Identifier: MIT-->
<!--Version: v1.0.0-->

# Collaborative Development

## Prerequisites
- [GitHub](https://github.com/tum-ens/pylovo) as the public repository. Please create an account.
- [Git](https://git-scm.com/) for version control. [Git How To](https://githowto.com/) and [Git Cheat Sheet](https://training.github.com/downloads/github-git-cheat-sheet.pdf) provide an introduction into working with Git.
- Development environment setup:
  - Clone the repository and set up the environment with [uv](https://docs.astral.sh/uv/) as described in the `README.rst` and in the installation page of the documentation: `uv sync` (add the extras you need, for example `--extra plots --extra api --extra docs`).
  - Copy `.env.example` to `.env` and enter the database connection. Never commit `.env`.
  - Requirements: Linux or WSL2, Python 3.12 or 3.13, GDAL (`ogr2ogr`) and a PostgreSQL database (version 15 or newer) with PostGIS and pgRouting.

### Optional: Setting Up SSH for GitHub
To simplify authentication, consider setting up SSH keys for GitHub. Follow [GitHub's guide on connecting with SSH](https://docs.github.com/en/authentication/connecting-to-github-with-ssh) for instructions.

## Types of interaction
This repository is following the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). <br>
Please be self-reflective and always maintain a good culture of discussion and active participation.

### A. Use
Since the open license allows free use, no notification is required. 
However, for the authors it is valuable information who uses the software for what purpose. 
Indicators are `Watch`, `Fork` and `Starred` of the repository. 

### B. Comment
You can give ideas, hints or report bugs in issues, in pull requests, at meetings or other channels. 
This is no development but can be considered a notable contribution. 
If you wish, add your name and details to [CITATION.cff](CITATION.cff).

### C. Contribute and Review
You add code and become an author of the repository. 
You must follow the workflow!
Add your name and details to [CITATION.cff](CITATION.cff).

### D. Maintain and Release
You contribute and take care of the repository. 
You review and answer questions. 
You coordinate and carry out the release.
Add your name and details to [CITATION.cff](CITATION.cff).

## Workflow
The workflow for contributing to this project has been inspired by the workflow described by [Vincent Driessen](https://nvie.com/posts/a-successful-git-branching-model/).

##### Branches and their purpose
* main - holds the released versions (tagged `vX.Y.Z`)
* dev - holds all current developments for the next release
* see other types below

### 1. Describe the issue on GitHub
Create an issue in the [GitHub repository](https://github.com/tum-ens/pylovo/issues). 
The `issue title` describes the problem you will address.  <br>
This is an important step as it forces one to think about the "issue".
Make a checklist for all needed steps if possible.
The issue templates in `.github/ISSUE_TEMPLATE` guide you to ensure a consistent description.

### 2. Solve the issue locally
There are two ways to create the branch:
1. Create a new branch from the `dev` branch
2. Create a new branch directly in GitHub from the issue

#### Naming convention for branches
Naming convention for branches: `type`/`issue-nr`-`short-description`

##### `type`
* feature - used for new features
* bugfix - used for bug fixes
* hotfix - used for quick fixes, should be branched from the `main` branch
* release - used for preparing a new release, should be branched from the `dev` branch

Note: The majority of the development will be done in `feature` branches.

##### `issue-nr`
The `issueNumber` should be taken from Step 1. Do not use the "#". 

##### `short-description`
Describe shortly what the branch is about. Usually, the title of the issue.

##### Other hints
- Separate type with `/` (slash)
- Separate other words with `-` (minus)
- Avoid using capital letters
- Do not put your name to the branch name, it's a collaborative project
- Branch names should be precise and informative

Examples of branch names: `feature/42-add-new-ontology-class`, `feature/911-branch-naming-convention`, `hotfix/404-update-api`, `release/v1.0.0`

#### 2.1. Create a new branch

##### Option 1: Get the latest version of the `dev` branch
1. Load the `dev` branch:
```bash
git checkout dev
```

2. Update with the latest version:
```bash
git pull
```

3. Create a new feature branch:
```bash
git checkout -b feature/1314-my-feature
```

##### Option 2: Create a new branch directly in GitHub from the issue
1. Click on `Create a branch` in the `Development` section of the issue.
2. Choose the appropriate source branch (most of the time `dev`).
3. Name the branch according to the naming convention.

#### 2.2. Start editing the files
- Divide your feature into small logical units
- Start to write the documentation or a docstring
- Don't rush, have the commit messages in mind
- Add your changes to the [CHANGELOG.md](CHANGELOG.md)

On first commit to the repo:
- Add your name and details to [CITATION.cff](CITATION.cff)

Check branch status:
```bash
git status
```

#### 2.3. Commit your changes 
If the file does not exist on the remote server yet, use:
```bash
git add filename.md
```

Then commit regularly with:
```bash
git commit filename.md
```

Write a good `commit message`:
- "If applied, this commit will ..."
- Follow [existing conventions for commit messages](https://chris.beams.io/posts/git-commit)
- Keep the subject line [shorter than 50 characters](https://chris.beams.io/posts/git-commit/#limit-50)
- Do not commit more than a few changes at the time: [atomic commits](https://en.wikipedia.org/wiki/Atomic_commit)
- Use [imperative mood](https://chris.beams.io/posts/git-commit/#imperative)
- Do not end the commit message with a [period](https://chris.beams.io/posts/git-commit/#end) ~~.~~ 
- Always end the commit message with the `issueNumber` including the "#"

Examples of commit message: `Added function with some method #42` or `Update documentation for commit messages #1`

#### 2.4 Fix your latest commit message
Do you want to improve your latest commit message? <br>
Is your latest commit not pushed yet? <br>
Edit the commit message of your latest commit:
```bash
git commit --amend
```

### 3. Maintain code

3.1. **Code Quality and Linting**:
To ensure consistency in code style and to prevent potential issues, check your code with [ruff](https://docs.astral.sh/ruff/) before you push:
```bash
uvx ruff check src
```

3.2. **Tests**:
The unit tests need no database. Run them with:
```bash
uv run --extra api pytest -q tests api/tests
```
Tests that need a database are skipped unless you name a disposable database for them. Never run the code against a database that holds data you want to keep: `pylovo-setup reset --database NAME` deletes all pylovo results.

3.3. **Documentation build**:
Build the documentation with warnings as errors, as the CI does:
```bash
uv run --extra docs sphinx-build -W --keep-going -b html docs/source docs/build/html
```

### 4. Push your commits
Push your `local` branch on the remote server `origin`. <br>
If your branch does not exist on the remote server yet, use:
```bash
git push --set-upstream origin feature/1314-my-feature
```

Then push regularly with:
```bash
git push
```

### 5. Open a pull request (PR)
Open a [pull request](https://github.com/tum-ens/pylovo/pulls) on GitHub with the pull request template of `.github/PULL_REQUEST_TEMPLATE`. <br>
The PR should be directed: `base: dev` <- `compare: feature/1314-my-feature`. <br>
Add the line `Closes #<issue-number>` in the description of your PR.
When it is merged, it automatically closes the issue. <br>
Assign a reviewer and get in contact. The CI builds the documentation, runs the unit tests and checks the HTTP API contract (`api/openapi.json`, see `api/README.md`).

#### 5.1. Let someone else review your PR
Assign one reviewer or a user group and get into contact.

If you are the reviewer:
- Check the changes in all corresponding files.
- Checkout the branch and run code.
- Comment if you would like to change something (Use `Request changes`)
- If all tests pass and all changes are good, `Approve` the PR. 
- Leave a comment and some nice words!

#### 5.2. Merge the PR and delete the feature branch
Merge the PR into `dev` and delete the feature branch.

### 6. Close the issue
Document the result in a few sentences and close the issue. <br>
Check that all steps have been documented:

- Issue title describes the problem you solved?
- The PR is linked to the issue?
- The branch was deleted?
- Entry in CHANGELOG.md?
- Tests and the documentation build pass?
- PR is merged?
- Issue is closed?

## Coding Standards

To maintain consistency and readability, please follow the coding standards outlined below when contributing to this project.

### Code Style

- **PEP 8**: All Python code should adhere to [PEP 8](https://www.python.org/dev/peps/pep-0008/) standards, which outline best practices for Python formatting, naming conventions, and structure. This includes:
  - Indentation (4 spaces per level).
  - Naming conventions for variables, functions, and classes.
  The code base does not enforce a line length.
  
- **Linting Tool**: To help ensure compliance, use [ruff](https://docs.astral.sh/ruff/):
  ```bash
  uvx ruff check src
  ```

- **Keep it simple**: Prefer the simplest solution. Do not add abstractions or options that nobody asked for. Configuration parameters stay as simple as possible and are documented with their shipped value in `docs/source/user_guide/configuration.rst`.

### Documentation Style
To ensure consistent and clear documentation, please follow the Google-style docstring format. 
This style is straightforward and well-suited for research projects, as it provides structured sections for describing parameters, return values, examples, and more.

#### General Rules
1. **Module-Level Docstring**: Each file should start with a top-level docstring explaining its purpose and any important details.
2. **Class and Function Docstrings**: Each class and function should have a docstring summarizing its behavior. Class docstrings should also document class attributes if applicable.
3. **Attributes**: Document module-level and class attributes with type annotations and descriptions. This can be done inline or in the `Attributes` section.

#### Docstring Format
- **Summary**: Begin with a concise summary of the object’s purpose.
- **Args**: List and describe each parameter, including type annotations and default values.
- **Returns**: Describe the return value(s) of the function, including type annotations. Use `Yields` instead of `Returns` for generator functions. Use `None` if the function returns nothing.
- **Raises**: List any exceptions that the function may raise.
- **Examples**: Provide examples of how to use the function or class.
- **Notes**: Include any additional information that may be helpful.
- **References**: Link to any external resources or references.
- **TODOs**: List any future work or improvements that could be made.

#### Examples of Each Type of Docstring
1. **Module-Level Docstring**
   ```python
   """Brief module description.
    
    This module demonstrates the Google-style docstring format. It provides
    several examples of documenting different types of objects and methods.
    
    Attributes:
        module_level_variable1 (int): Module level variables can be documented here.
        module_level_variable2 (str): An example of an inline attribute docstring.
    """
   ```

2. **Function Docstring**
    ```python
    def example_function(param1: int, param2: str = 'default') -> bool:
         """Summary of the function.
         
         Args:
              param1 (int): The first parameter.
              param2 (str): The second parameter. Defaults to 'default'.
         
         Returns:
              bool: A boolean value indicating success.
         
         Raises:
              ValueError: If the parameter is invalid.
         """
    ```

3. **Generator Function Docstring**
    ```python
    def example_generator(param: int) -> Iterator[int]:
         """Summary of the generator function.
         
         Args:
              param (int): The parameter for the generator.
         
         Yields:
              int: The next value in the sequence.
         """
    ```
   
4. **Class Docstring with Attributes**
    ```python
    class ExampleClass:
    """A summary line for the class.
    
        Attributes:
            attr1 (str): Description of `attr1`.
            attr2 (int, optional): Description of `attr2`, which is optional.
        """
    
        def __init__(self, attr1: str, attr2: int = 0):
            """Initializes ExampleClass with specified attributes.
    
            Args:
                attr1 (str): Description of `attr1`.
                attr2 (int, optional): Description of `attr2`. Defaults to 0.
            """
            self.attr1 = attr1
            self.attr2 = attr2
    ```

5. **Exception Docstring**
    ```python
    class CustomError(Exception):
        """Exception raised for specific errors in ExampleClass.
    
        Args:
            msg (str): Explanation of the error.
            code (int, optional): Error code, if applicable.
    
        Attributes:
            msg (str): Explanation of the error.
            code (int): Error code.
        """
        def __init__(self, msg: str, code: int = None):
            self.msg = msg
            self.code = code
    ```

6. **Special Methods and Private Methods**
- Document special methods like `__init__`, `__str__`, and `__repr__` in the class docstring or their individual docstrings as you would any other method.
- Private methods (starting with `_`) generally do not require detailed docstrings unless they are complex.

#### Notes
- **Examples**: When adding examples use doctest format where possible. This allows you to test the examples as part of your test suite.
- **Formatting Tips**:
    - Use triple quotes for multi-line docstrings.
    - Use indentation consistently for multiline parameter descriptions.
    - Avoid documenting `self` in `Args` for instance methods, as it is implied.
    - Use `None` for default values instead of `NoneType`.
    - Use `Optional` for optional parameters with default values.
    - For properties, document only in the getter method.

Following this format will ensure all contributors create clear, structured documentation that can be easily read and maintained by others.

## Updating Documentation and Changelog
Keeping documentation up-to-date is essential for ensuring other contributors and users understand new features, changes, and fixes. Please follow these guidelines when updating documentation and the changelog.

### 1. Updating Documentation
- **Main Documentation**: If your changes introduce new features or modify existing functionality, update the relevant sections in the documentation located in the docs/ folder.
  - **Writing Style**: Keep the writing clear, concise, and objective.
  - **Code Examples**: Include code examples where applicable to demonstrate usage.
  - **Building Documentation**: The documentation is built with Sphinx. Preview changes by building it locally, with warnings as errors as in the CI:
    ```bash
    uv run --extra docs sphinx-build -W --keep-going -b html docs/source docs/build/html
    ```
  - **Figures**: The figures come from the scripts in `docs/scripts` and are committed (see the development page of the documentation).
- **Docstrings**: Update or add docstrings to any new or modified functions, classes, or modules, following the Google-style guidelines in the Documentation Style section above.

### 2. Updating Changelog
The `CHANGELOG.md` file keeps a record of significant changes to the project and helps users and contributors track updates. Please follow these conventions for maintaining the changelog:

- **Entry Format**:
  - New entries go under `## [Unreleased]`. At a release the maintainers move them to a section `## [X.Y.Z] - YYYY-MM-DD`.
  - Use the following structure for each section:
    ```markdown
    ## [Version] - [YYYY-MM-DD]
    
    ### Added
    - Brief description of the new feature, referencing the issue or pull request number if applicable.
    
    ### Changed
    - Description of any modifications to existing features.
    
    ### Fixed
    - Description of bug fixes or corrections.
    
    ### Removed
    - Deprecated features or removed functionality.
    ```
  - Name configuration keys, commands and migrations exactly, and say what a user has to do after an upgrade.
- **Semantic Versioning**: Follow [Semantic Versioning](https://semver.org/) guidelines when updating the version number. Increment the version based on the impact of the changes:
  - **Major**: Breaking changes or significant new features.
  - **Minor**: New features or enhancements that are backward-compatible.
  - **Patch**: Bug fixes or minor improvements that do not affect compatibility.

### 3. Reviewing Documentation and Changelog Before Submission
- **Double-Check**: Ensure all new functionality is covered in the documentation, and verify that changelog entries are clear and concise.
- **Consistency**: Use consistent terminology and formatting across all documentation and changelog entries.
- **Preview Changes**: When possible, preview the documentation and changelog to ensure they are visually correct and well-organized.

By following these guidelines, you’ll help maintain high-quality, organized documentation that benefits all users and contributors.
