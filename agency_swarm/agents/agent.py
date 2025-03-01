import copy
import inspect
import json
import os
from typing import Dict, Literal, Union, Any, Type
from typing import List

from deepdiff import DeepDiff

from openai import NotFoundError
from openai.types.beta.assistant import ToolResources
from agency_swarm.tools import BaseTool, ToolFactory
from agency_swarm.tools import Retrieval, CodeInterpreter, FileSearch
from agency_swarm.util.oai import get_openai_client
from agency_swarm.util.openapi import validate_openapi_spec

from agency_swarm.threads import Thread

class Agent():
    @property
    def assistant(self):
        if self._assistant is None:
            raise Exception("Assistant is not initialized. Please run init_oai() first.")
        return self._assistant

    @assistant.setter
    def assistant(self, value):
        self._assistant = value

    @property
    def functions(self):
        return [tool for tool in self.tools if issubclass(tool, BaseTool)]

    @property
    def threads(self) -> List[Thread]:
        return self._threads  # 返回数组的内容

    def add_thread(self, thread:Thread):
        ## Mutex
        if thread not in self._threads:
            self._threads.append(thread)  # 提供一个方法来追加项目到数组
        ## Mutex
    def remove_thread(self, thread:Thread):
        ## Mutex
        self._threads.remove(thread)  # 提供一个方法来追加项目到数组
        ## Mutex

    def response_validator(self, message: str) -> str:
        """
        Validates the response from the agent. If the response is invalid, it must raise an exception with instructions
        for the caller agent on how to proceed.

        Parameters:
            message (str): The response from the agent.

        Returns:
            str: The validated response.
        """
        return message

    def __init__(self, 
                 id: str = None, 
                 name: str = None, 
                 description: str = None, 
                 instructions: str = "",
                 tools: List[Union[Type[BaseTool],Type[FileSearch], Type[Retrieval], Type[CodeInterpreter]]] = None,
                 tool_resources: ToolResources = None,                 
                 files_folder: Union[List[str], str] = None, 
                 schemas_folder: Union[List[str], str] = None,
                 api_headers: Dict[str, Dict[str, str]] = None, 
                 api_params: Dict[str, Dict[str, str]] = None,
                 file_ids: List[str] = None, 
                 metadata: Dict[str, str] = None, 
                 model: str = "gpt-4-1106-preview"):
        """
        Initializes an Agent with specified attributes, tools, and OpenAI client.

        Parameters:
        id (str, optional): Loads the assistant from OpenAI assistant ID. Assistant will be created or loaded from settings if ID is not provided. Defaults to None.
        name (str, optional): Name of the agent. Defaults to the class name if not provided.
        description (str, optional): A brief description of the agent's purpose. Defaults to None.
        instructions (str, optional): Path to a file containing specific instructions for the agent. Defaults to an empty string.
        tools (List[Union[Type[BaseTool], Type[Retrieval], Type[CodeInterpreter]]], optional): A list of tools (as classes) that the agent can use. Defaults to an empty list.
        tool_resources (ToolResources, optional): A set of resources that are used by the assistant's tools. The resources are specific to the type of tool. For example, the code_interpreter tool requires a list of file IDs, while the file_search tool requires a list of vector store IDs. Defaults to None.
        files_folder (Union[List[str], str], optional): Path or list of paths to directories containing files associated with the agent. Defaults to None.
        schemas_folder (Union[List[str], str], optional): Path or list of paths to directories containing OpenAPI schemas associated with the agent. Defaults to None.
        api_headers (Dict[str,Dict[str, str]], optional): Headers to be used for the openapi requests. Each key must be a full filename from schemas_folder. Defaults to an empty dictionary.
        api_params (Dict[str, Dict[str, str]], optional): Extra params to be used for the openapi requests. Each key must be a full filename from schemas_folder. Defaults to an empty dictionary.
        file_ids (List[str], optional): List of file IDs for files associated with the agent. Defaults to an empty list.
        metadata (Dict[str, str], optional): Metadata associated with the agent. Defaults to an empty dictionary.
        model (str, optional): The model identifier for the OpenAI API. Defaults to "gpt-4-1106-preview".

        This constructor sets up the agent with its unique properties, initializes the OpenAI client, reads instructions if provided, and uploads any associated files.
        """
        # public attributes
        self.id = id
        self.name = name if name else self.__class__.__name__
        self.description = description
        self.instructions = instructions
        self.tools = tools[:] if tools is not None else []
        self.tools = [tool for tool in self.tools if tool.__name__ != "ExampleTool"]
        self.tool_resources = tool_resources  # new
        self.files_folder = files_folder if files_folder else []
        self.schemas_folder = schemas_folder if schemas_folder else []
        self.api_headers = api_headers if api_headers else {}
        self.api_params = api_params if api_params else {}
        self.file_ids = file_ids if file_ids else []
        self.metadata = metadata if metadata else {}
        self.model = model

        # private attributes
        self._assistant: Any = None
        self._shared_instructions = None
        self._threads = []

        # init methods
        self.client = get_openai_client()
        self._read_instructions()

        # deal with files
        self._upload_files()
        if file_ids:
            print("Warning: 'file_ids' parameter is deprecated. Please use 'tool_resources' parameter instead.")
            self.add_file_ids(file_ids, "file_search")
        self._parse_schemas()

    # --- OpenAI Assistant Methods ---

    def init_oai(self):
        """
        Initializes the OpenAI assistant for the agent.

        This method handles the initialization and potential updates of the agent's OpenAI assistant. It loads the assistant based on a saved ID, updates the assistant if necessary, or creates a new assistant if it doesn't exist. After initialization or update, it saves the assistant's settings.

        Output:
        self: Returns the agent instance for chaining methods or further processing.
        """

        # check if settings.json exists
        path = self.get_settings_path()

        # load assistant from id
        if self.id:
            self.assistant = self.client.beta.assistants.retrieve(self.id)
            self.instructions = self.assistant.instructions
            self.name = self.assistant.name
            self.description = self.assistant.description
            self.file_ids = self.assistant.file_ids
            self.metadata = self.assistant.metadata
            self.model = self.assistant.model
            self.tool_resources = self.assistant.tool_resources.model_dump()
            # update assistant if parameters are different
            if not self._check_parameters(self.assistant.model_dump()):
                self._update_assistant()
            return self

        # load assistant from settings
        if os.path.exists(path):
            with open(path, 'r') as f:
                settings = json.load(f)
                # iterate settings and find the assistant with the same name
                for assistant_settings in settings:
                    if assistant_settings['name'] == self.name:
                        try:
                            self.assistant = self.client.beta.assistants.retrieve(assistant_settings['id'])
                            self.id = assistant_settings['id']
                            if self.assistant.tool_resources:
                                self.tool_resources = self.assistant.tool_resources.model_dump()
                            # update assistant if parameters are different
                            if not self._check_parameters(self.assistant.model_dump()):
                                print("Updating assistant... " + self.name)
                                self._update_assistant()
                            self._update_settings()
                            return self
                        except NotFoundError:
                            print('not found assistant')
                            continue
        # create assistant if settings.json does not exist or assistant with the same name does not exist
        self.assistant = self.client.beta.assistants.create(
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            tools=self.get_oai_tools(),
            tool_resources=self.tool_resources,
            metadata=self.metadata,
            model=self.model
        )

        if self.assistant.tool_resources:
            self.tool_resources = self.assistant.tool_resources.model_dump()

        self.id = self.assistant.id

        self._save_settings()

        return self

    def _update_assistant(self):
        """
        Updates the existing assistant's parameters on the OpenAI server.

        This method updates the assistant's details such as name, description, instructions, tools, file IDs, metadata, and the model. It only updates parameters that have non-empty values. After updating the assistant, it also updates the local settings file to reflect these changes.

        No input parameters are directly passed to this method as it uses the agent's instance attributes.

        No output parameters are returned, but the method updates the assistant's details on the OpenAI server and locally updates the settings file.
        """

        params = {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "tools": self.get_oai_tools(),
            "tool_resources": self.tool_resources,
            "file_ids": self.file_ids,
            "metadata": self.metadata,
            "model": self.model
        }
        params = {k: v for k, v in params.items() if v}
        self.assistant = self.client.beta.assistants.update(
            self.id,
            **params,
        )
        self._update_settings()

    def _upload_files(self):
        def add_id_to_file(f_path, id):
            """Add file id to file name"""
            if os.path.isfile(f_path):
                file_name, file_ext = os.path.splitext(f_path)
                f_path_new = file_name + "_" + id + file_ext
                os.rename(f_path, f_path_new)
                return f_path_new
            else:
                raise Exception("Items in files folder must be files.")

        def get_id_from_file(f_path):
            """Get file id from file name"""
            if os.path.isfile(f_path):
                file_name, file_ext = os.path.splitext(f_path)
                file_name = os.path.basename(file_name)
                file_name = file_name.split("_")
                if len(file_name) > 1:
                    return file_name[-1] if "file-" in file_name[-1] else None
                else:
                    return None
            else:
                raise Exception("Items in files folder must be files.")

        files_folders = self.files_folder if isinstance(self.files_folder, list) else [self.files_folder]

        file_search_ids = []
        code_interpreter_ids = []

        for files_folder in files_folders:
            if isinstance(files_folder, str):
                f_path = files_folder

                if not os.path.isdir(f_path):
                    f_path = os.path.join(self.get_class_folder_path(), files_folder)
                    f_path = os.path.normpath(f_path)

                if os.path.isdir(f_path):
                    f_paths = os.listdir(f_path)

                    f_paths = [f for f in f_paths if not f.startswith(".")]

                    f_paths = [os.path.join(f_path, f) for f in f_paths]

                    code_interpreter_file_extensions = [
                        ".json",  # JSON
                        ".csv",  # CSV
                        ".xml",  # XML
                        ".jpeg",  # JPEG
                        ".jpg",  # JPEG
                        ".gif",  # GIF
                        ".png",  # PNG
                        ".zip"  # ZIP
                    ]

                    # uploading files
                    for f_path in f_paths:
                        file_extend = os.path.splitext(f_path)[1]

                        f_path = f_path.strip()
                        file_id = get_id_from_file(f_path)
                        if file_id:
                            print("File already uploaded. Skipping... " + os.path.basename(f_path))
                            self.file_ids.append(file_id)
                        else:
                            print("Uploading new file... " + os.path.basename(f_path))
                            with open(f_path, 'rb') as f:
                                file_id = self.client.files.create(
                                    file=f, 
                                    purpose="assistants").id
                                self.file_ids.append(file_id)
                                f.close()
                            add_id_to_file(f_path, file_id)
                        if file_extend in code_interpreter_file_extensions:
                            code_interpreter_ids.append(file_id)
                        else:
                            file_search_ids.append(file_id)
                else:
                    raise Exception("Files folder path is not a directory.")
            else:
                raise Exception("Files folder path must be a string or list of strings.")

        if FileSearch not in self.tools and file_search_ids:
            print("Detected files without FileSearch. Adding FileSearch tool...")
            self.add_tool(FileSearch)
        if CodeInterpreter not in self.tools and code_interpreter_ids:
            print("Detected files without FileSearch. Adding FileSearch tool...")
            self.add_tool(CodeInterpreter)

        self.add_file_ids(file_search_ids, "file_search")
        self.add_file_ids(code_interpreter_ids, "code_interpreter")

    # --- Tool Methods ---

    def add_tool(self, tool):
        if not isinstance(tool, type):
            raise Exception("Tool must not be initialized.")
        if issubclass(tool, Retrieval):
            # check that tools name is not already in tools
            for t in self.tools:
                if issubclass(t, Retrieval):
                    return
            self.tools.append(tool)
        elif issubclass(tool, FileSearch):
            # check that tools name is not already in tools
            for t in self.tools:
                if issubclass(t, FileSearch):
                    return
            self.tools.append(tool)
        elif issubclass(tool, CodeInterpreter):
            for t in self.tools:
                if issubclass(t, Retrieval):
                    return
            self.tools.append(tool)
        elif issubclass(tool, BaseTool):
            for t in self.tools:
                if t.__name__ == tool.__name__:
                    self.tools.remove(t)
            self.tools.append(tool)
        else:
            raise Exception("Invalid tool type.")

    def get_oai_tools(self):
        tools = []
        for tool in self.tools:
            if not isinstance(tool, type):
                print(tool)
                raise Exception("Tool must not be initialized.")

            if issubclass(tool, Retrieval):
                tools.append(tool().model_dump())
            elif issubclass(tool, FileSearch):
                tools.append(tool().model_dump())
            elif issubclass(tool, CodeInterpreter):
                tools.append(tool().model_dump())
            elif issubclass(tool, BaseTool):
                tools.append({
                    "type": "function",
                    "function": tool.openai_schema
                })
            else:
                raise Exception("Invalid tool type.")
        return tools

    def _parse_schemas(self):
        schemas_folders = self.schemas_folder if isinstance(self.schemas_folder, list) else [self.schemas_folder]

        for schemas_folder in schemas_folders:
            if isinstance(schemas_folder, str):
                f_path = schemas_folder

                if not os.path.isdir(f_path):
                    f_path = os.path.join(self.get_class_folder_path(), schemas_folder)

                if os.path.isdir(f_path):
                    f_paths = os.listdir(f_path)

                    f_paths = [f for f in f_paths if not f.startswith(".")]

                    f_paths = [os.path.join(f_path, f) for f in f_paths]

                    for f_path in f_paths:
                        with open(f_path, 'r') as f:
                            openapi_spec = f.read()
                            f.close()
                        try:
                            validate_openapi_spec(openapi_spec)
                        except Exception as e:
                            print("Invalid OpenAPI schema: " + os.path.basename(f_path))
                            raise e
                        try:
                            headers = None
                            params = None
                            if os.path.basename(f_path) in self.api_headers:
                                headers = self.api_headers[os.path.basename(f_path)]
                            if os.path.basename(f_path) in self.api_params:
                                params = self.api_params[os.path.basename(f_path)]
                            tools = ToolFactory.from_openapi_schema(openapi_spec, headers=headers, params=params)
                        except Exception as e:
                            print("Error parsing OpenAPI schema: " + os.path.basename(f_path))
                            raise e
                        for tool in tools:
                            self.add_tool(tool)
                else:
                    raise Exception("Schemas folder path is not a directory.")
            else:
                raise Exception("Schemas folder path must be a string or list of strings.")

    # --- Settings Methods ---

    def _check_parameters(self, assistant_settings):
        """
        Checks if the agent's parameters match with the given assistant settings.

        Parameters:
        assistant_settings (dict): A dictionary containing the settings of an assistant.

        Returns:
        bool: True if all the agent's parameters match the assistant settings, False otherwise.

        This method compares the current agent's parameters such as name, description, instructions, tools, file IDs, metadata, and model with the given assistant settings. It uses DeepDiff to compare complex structures like tools and metadata. If any parameter does not match, it returns False; otherwise, it returns True.
        """

        if self.name != assistant_settings['name']:
            return False
        
        if self.description != assistant_settings['description']:
            return False
        
        if self.instructions != assistant_settings['instructions']:
            return False
        
        tools_diff = DeepDiff(self.get_oai_tools(), assistant_settings['tools'], ignore_order=True)
        if tools_diff != {}:
            return False
        
        tool_resources_settings = copy.deepcopy(self.tool_resources)
        if tool_resources_settings and tool_resources_settings.get('file_search'):
            tool_resources_settings['file_search'].pop('vector_stores', None)
        tool_resources_diff = DeepDiff(tool_resources_settings, assistant_settings['tool_resources'], ignore_order=True)
        if tool_resources_diff != {}:
            return False
        
        metadata_diff = DeepDiff(self.metadata, assistant_settings['metadata'], ignore_order=True)
        if metadata_diff != {}:
            return False
        
        if self.model != assistant_settings['model']:
            return False
        
        return True

    def _save_settings(self):
        path = self.get_settings_path()
        # check if settings.json exists
        if not os.path.isfile(path):
            with open(path, 'w') as f:
                json.dump([self.assistant.model_dump()], f, indent=4)
        else:
            settings = []
            with open(path, 'r') as f:
                settings = json.load(f)
                settings.append(self.assistant.model_dump())
            with open(path, 'w') as f:
                json.dump(settings, f, indent=4)

    def _update_settings(self):
        path = self.get_settings_path()
        # check if settings.json exists
        if os.path.isfile(path):
            settings = []
            with open(path, 'r') as f:
                settings = json.load(f)
                for i, assistant_settings in enumerate(settings):
                    if assistant_settings['id'] == self.id:
                        settings[i] = self.assistant.model_dump()
                        break
            with open(path, 'w') as f:
                json.dump(settings, f, indent=4)

    # --- Helper Methods ---

    def add_file_ids(self, file_ids: List[str], tool_resource: Literal["code_interpreter", "file_search"]):
        if not file_ids:
            return
        
        if self.tool_resources is None:
            self.tool_resources = {}

        if tool_resource == "code_interpreter":
            if CodeInterpreter not in self.tools:
                raise Exception("CodeInterpreter tool not found in tools.")

            if tool_resource not in self.tool_resources or self.tool_resources[
                tool_resource] is None:
                self.tool_resources[tool_resource] = {
                    "file_ids": file_ids
                }

            self.tool_resources[tool_resource]['file_ids'] = file_ids
        elif tool_resource == "file_search":
            if FileSearch not in self.tools:
                raise Exception("FileSearch tool not found in tools.")

            if tool_resource not in self.tool_resources or self.tool_resources[
                tool_resource] is None:
                self.tool_resources[tool_resource] = {
                    "vector_stores": [{
                        "file_ids": file_ids
                    }]
                }
            elif not self.tool_resources[tool_resource].get('vector_store_ids'):
                self.tool_resources[tool_resource]['vector_stores'] = [{
                    "file_ids": file_ids
                }]
            else:
                vector_store_id = self.tool_resources[tool_resource]['vector_store_ids'][0]
                self.client.beta.vector_stores.file_batches.create(
                    vector_store_id=vector_store_id,
                    file_ids=file_ids
                )
        else:
            raise Exception("Invalid tool resource.")

    def get_settings_path(self):
        return os.path.join("./", 'settings.json')

    def _read_instructions(self):
        if os.path.isfile(self.instructions):
            with open(self.instructions, 'r') as f:
                self.instructions = f.read()
        elif os.path.isfile(os.path.join(self.get_class_folder_path(), self.instructions)):
            with open(os.path.join(self.get_class_folder_path(), self.instructions), 'r') as f:
                self.instructions = f.read()

    def get_class_folder_path(self):
        return os.path.abspath(os.path.dirname(inspect.getfile(self.__class__)))

    def add_shared_instructions(self, instructions: str):
        if self._shared_instructions is None:
            self._shared_instructions = instructions
        else:
            self.instructions = self.instructions.replace(self._shared_instructions, "")
            self.instructions = self.instructions.strip().strip("\n")
            self._shared_instructions = instructions

        self.instructions = self._shared_instructions + "\n\n" + self.instructions

    # --- Cleanup Methods ---
    def delete(self):
        self._delete_assistant()
        self._delete_files()
        self._delete_settings()

    def _delete_files(self):
        if not self.tool_resources:
            return

        file_ids = []
        if self.tool_resources.get('code_interpreter'):
            file_ids = self.tool_resources['code_interpreter'].get('file_ids', [])

        if self.tool_resources.get('file_search'):
            file_search_vector_store_ids = self.tool_resources['file_search'].get('vector_store_ids', [])
            for vector_store_id in file_search_vector_store_ids:
                files = self.client.beta.vector_stores.files.list(vector_store_id=vector_store_id, limit=100)
                for file in files:
                    file_ids.append(file.id)

                self.client.beta.vector_stores.delete(vector_store_id)

        for file_id in file_ids:
            self.client.files.delete(file_id)

    def _delete_assistant(self):
        self.client.beta.assistants.delete(self.id)
        self._delete_settings()

    def _delete_settings(self):
        path = self.get_settings_path()
        # check if settings.json exists
        if os.path.isfile(path):
            settings = []
            with open(path, 'r') as f:
                settings = json.load(f)
                for i, assistant_settings in enumerate(settings):
                    if assistant_settings['id'] == self.id:
                        settings.pop(i)
                        break
            with open(path, 'w') as f:
                json.dump(settings, f, indent=4)
