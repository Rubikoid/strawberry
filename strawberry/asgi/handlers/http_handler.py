from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Optional

from starlette import status
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response

from strawberry.exceptions import MissingQueryError
from strawberry.file_uploads.utils import replace_placeholders_with_files
from strawberry.http import parse_query_params, parse_request_data
from strawberry.schema.exceptions import InvalidOperationTypeError
from strawberry.types.graphql import OperationType
from strawberry.utils.debug import pretty_print_graphql_operation
from strawberry.utils.graphiql import get_graphiql_html

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send

    from strawberry.schema import BaseSchema
    from strawberry.types.execution import ExecutionResult


class HTTPHandler:
    def __init__(
        self,
        schema: BaseSchema,
        graphiql: bool,
        allow_queries_via_get: bool,
        debug: bool,
        get_context,
        get_root_value,
        process_result,
        encode_json,
    ):
        self.schema = schema
        self.graphiql = graphiql
        self.allow_queries_via_get = allow_queries_via_get
        self.debug = debug
        self.get_context = get_context
        self.get_root_value = get_root_value
        self.process_result = process_result
        self.encode_json = encode_json

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope=scope, receive=receive)
        root_value = await self.get_root_value(request)

        sub_response = Response()
        sub_response.status_code = None  # type: ignore
        del sub_response.headers["content-length"]

        context = await self.get_context(request=request, response=sub_response)

        response = await self.get_http_response(
            request=request,
            execute=self.execute,
            process_result=self.process_result,
            root_value=root_value,
            context=context,
        )

        response.headers.raw.extend(sub_response.headers.raw)

        if sub_response.background:
            response.background = sub_response.background

        if sub_response.status_code:
            response.status_code = sub_response.status_code

        await response(scope, receive, send)

    async def get_http_response(
        self,
        request: Request,
        execute: Callable,
        process_result: Callable,
        root_value: Optional[Any],
        context: Optional[Any],
    ) -> Response:
        method = request.method

        if method == "GET":
            if request.query_params:
                try:
                    data = parse_query_params(request.query_params._dict)
                except json.JSONDecodeError:
                    return PlainTextResponse(
                        "Unable to parse request body as JSON",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

            elif self.should_render_graphiql(request):
                return self.get_graphiql_response()
            else:
                return HTMLResponse(status_code=status.HTTP_404_NOT_FOUND)
        elif method == "POST":
            content_type = request.headers.get("Content-Type", "")
            if "application/json" in content_type:
                try:
                    data = await request.json()
                except json.JSONDecodeError:
                    return PlainTextResponse(
                        "Unable to parse request body as JSON",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
            elif content_type.startswith("multipart/form-data"):
                multipart_data = await request.form()
                try:
                    operations_text = multipart_data.get("operations", "{}")
                    operations = json.loads(operations_text)  # type: ignore
                    files_map = json.loads(multipart_data.get("map", "{}"))  # type: ignore # noqa: E501
                except json.JSONDecodeError:
                    return PlainTextResponse(
                        "Unable to parse request body as JSON",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )

                try:
                    data = replace_placeholders_with_files(
                        operations, files_map, multipart_data
                    )
                except KeyError:
                    return PlainTextResponse(
                        "File(s) missing in form data",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
            else:
                return PlainTextResponse(
                    "Unsupported Media Type",
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                )
        else:
            return PlainTextResponse(
                "Method Not Allowed",
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            )

        try:
            request_data = parse_request_data(data)
        except json.JSONDecodeError:
            return PlainTextResponse(
                "Unable to parse request body as JSON",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        allowed_operation_types = OperationType.from_http(method)

        if not self.allow_queries_via_get and method == "GET":
            allowed_operation_types = allowed_operation_types - {OperationType.QUERY}

        try:
            result = await execute(
                request_data.query,
                variables=request_data.variables,
                context=context,
                operation_name=request_data.operation_name,
                root_value=root_value,
                allowed_operation_types=allowed_operation_types,
            )
        except InvalidOperationTypeError as e:
            return PlainTextResponse(
                e.as_http_error_reason(method),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        except MissingQueryError:
            return PlainTextResponse(
                "No GraphQL query found in the request",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        response_data = await process_result(request=request, result=result)

        return Response(
            self.encode_json(response_data),
            status_code=status.HTTP_200_OK,
            media_type="application/json",
        )

    def should_render_graphiql(self, request: Request) -> bool:
        if not self.graphiql:
            return False

        return any(
            supported_header in request.headers.get("accept", "")
            for supported_header in ("text/html", "*/*")
        )

    def get_graphiql_response(self) -> HTMLResponse:
        html = get_graphiql_html()

        return HTMLResponse(html)

    async def execute(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        context: Any = None,
        operation_name: Optional[str] = None,
        root_value: Any = None,
        allowed_operation_types: Optional[Iterable[OperationType]] = None,
    ) -> ExecutionResult:
        if self.debug:
            pretty_print_graphql_operation(operation_name, query, variables)

        return await self.schema.execute(
            query,
            root_value=root_value,
            variable_values=variables,
            operation_name=operation_name,
            context_value=context,
            allowed_operation_types=allowed_operation_types,
        )
